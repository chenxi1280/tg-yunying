from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, GroupBotAdmission

from .runtime_state_hash import canonical_state_hash


TYPED_REMOTE_FACTS = {
    "ensure_channel_membership": "membership_observed",
    "ensure_target_membership": "membership_observed",
    "group_bot_channel_follow": "group_bot_channel_follow",
    "group_bot_confirmation_button": "group_bot_confirmation_button",
}


def typed_remote_fact_id(
    action: Action,
    attempt: ExecutionAttempt,
    fact_type: str,
) -> str:
    request_identity = str(
        (attempt.result_snapshot or {}).get("gateway_request_identity")
        or (action.result or {}).get("gateway_request_identity")
        or ""
    )
    if not request_identity:
        raise RuntimeError("gateway_request_identity_missing")
    fingerprint = canonical_state_hash({
        "fact_type": fact_type,
        "action_id": action.id,
        "attempt_id": attempt.id,
        "account_id": attempt.account_id,
        "request_identity": request_identity,
        "payload": action.payload or {},
    })
    return f"{fact_type}:{fingerprint}"


def apply_confirmed_business_fact(
    session: Session,
    action: Action,
    attempt: ExecutionAttempt,
    *,
    result: str,
    remote_fact_id: str,
) -> None:
    if result != "remote_confirmed":
        return
    fact_type = TYPED_REMOTE_FACTS.get(action.action_type)
    if fact_type is None:
        return
    expected = typed_remote_fact_id(action, attempt, fact_type)
    if remote_fact_id != expected:
        raise RuntimeError("typed_remote_fact_identity_mismatch")
    if fact_type == "membership_observed":
        _confirm_membership_fact(session, action)
        return
    if fact_type == "group_bot_channel_follow":
        _confirm_group_bot_follow_fact(session, action)
        return
    _confirm_group_bot_callback_fact(session, action)


def _confirm_membership_fact(session: Session, action: Action) -> None:
    from .channel_membership import mark_channel_membership_joined

    payload = action.payload if isinstance(action.payload, dict) else {}
    target_id = int(payload.get("channel_target_id") or 0)
    if not target_id or not action.account_id:
        raise RuntimeError("membership_remote_fact_target_missing")
    label = "可发言" if payload.get("require_send") else "已关注"
    mark_channel_membership_joined(
        session,
        action.tenant_id,
        target_id,
        action.account_id,
        permission_label=label,
    )
    action.result = {
        **dict(action.result or {}),
        "membership_status": "recovered_after_unknown",
        "validation_stage": "remote_reconcile_membership_probe",
    }


def _confirm_group_bot_follow_fact(session: Session, action: Action) -> None:
    from .group_bot_admission import mark_channel_follow_completed

    payload = action.payload if isinstance(action.payload, dict) else {}
    admission = session.get(GroupBotAdmission, int(payload.get("admission_id") or 0))
    _require_matching_admission(action, admission, payload)
    channel_ref = str(payload.get("channel_ref") or "").strip().lstrip("@")
    if not channel_ref:
        raise RuntimeError("group_bot_follow_remote_fact_target_missing")
    mark_channel_follow_completed(
        session,
        admission=admission,
        channel_ref=channel_ref,
        resolved_peer_id=channel_ref,
        resolved_type="broadcast",
        action_id=str(action.id),
    )
    action.result = {
        **dict(action.result or {}),
        "channel_ref": channel_ref,
        "group_bot_admission_id": admission.id,
        "group_bot_admission_state": admission.state,
    }


def _confirm_group_bot_callback_fact(session: Session, action: Action) -> None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    admission = session.get(GroupBotAdmission, int(payload.get("admission_id") or 0))
    _require_matching_admission(action, admission, payload)
    action.result = {
        **dict(action.result or {}),
        "group_bot_admission_id": admission.id,
        "group_bot_admission_state": admission.state,
        "confirmation_click": "accepted_waiting_bot_confirmation",
    }


def _require_matching_admission(
    action: Action,
    admission: GroupBotAdmission | None,
    payload: dict,
) -> None:
    if admission is None or admission.tenant_id != action.tenant_id:
        raise RuntimeError("group_bot_remote_fact_admission_missing")
    if int(admission.account_id) != int(action.account_id or 0):
        raise RuntimeError("group_bot_remote_fact_account_mismatch")
    if int(admission.group_id) != int(payload.get("group_id") or 0):
        raise RuntimeError("group_bot_remote_fact_group_mismatch")
    if int(admission.admission_version or 1) != int(payload.get("admission_version") or 1):
        raise RuntimeError("group_bot_remote_fact_version_mismatch")
    if action.action_type != "group_bot_confirmation_button":
        return
    if admission.trusted_bot_peer_id != str(payload.get("trusted_bot_peer_id") or ""):
        raise RuntimeError("group_bot_remote_fact_peer_mismatch")
    if admission.source_message_id != str(payload.get("source_message_id") or ""):
        raise RuntimeError("group_bot_remote_fact_source_mismatch")


__all__ = ["apply_confirmed_business_fact", "typed_remote_fact_id"]
