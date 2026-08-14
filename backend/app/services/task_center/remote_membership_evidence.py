from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, OperationTarget, RemoteReconcileCase, TgAccount

from .payloads import EnsureChannelMembershipPayload
from .remote_reconciliation import (
    RemoteReconcileEvidence,
    typed_remote_fact_id,
)
from .runtime_state_hash import canonical_state_hash


MEMBERSHIP_ACTION_TYPES = frozenset({
    "ensure_channel_membership",
    "ensure_target_membership",
})


def preview_membership_probe_evidence(
    session: Session,
    case_id: str,
    *,
    gateway_client,
    credentials_resolver,
) -> RemoteReconcileEvidence:
    case, action, attempt = _case_facts(session, case_id)
    payload = _membership_payload(action)
    account = session.get(TgAccount, action.account_id)
    target = session.get(OperationTarget, payload.channel_target_id) if payload else None
    if not _identity_is_valid(action, attempt, account, target, payload):
        return _inconclusive(case, "membership_probe_identity_mismatch")
    try:
        result = gateway_client.probe_target_capabilities(
            account.id,
            payload.channel_id,
            payload.target_type,
            account.session_ciphertext,
            credentials_resolver(session, account),
            require_send=payload.require_send,
        )
    except Exception as exc:  # noqa: BLE001 - the exception class is explicit evidence.
        return _inconclusive(case, f"membership_probe_error_{type(exc).__name__}")
    if not result.ok:
        return _inconclusive(case, "membership_probe_not_confirmed")
    return _confirmed_evidence(action, attempt)


def _case_facts(
    session: Session,
    case_id: str,
) -> tuple[RemoteReconcileCase, Action, ExecutionAttempt]:
    case = session.get(RemoteReconcileCase, case_id)
    if case is None:
        raise ValueError("remote_reconcile_case_not_found")
    action = session.get(Action, case.action_id)
    attempt = session.get(ExecutionAttempt, case.execution_attempt_id)
    if action is None or attempt is None:
        raise RuntimeError("remote_reconcile_fact_incomplete")
    return case, action, attempt


def _membership_payload(action: Action) -> EnsureChannelMembershipPayload | None:
    if action.action_type not in MEMBERSHIP_ACTION_TYPES:
        return None
    try:
        return EnsureChannelMembershipPayload.model_validate(action.payload or {})
    except ValueError:
        return None


def _identity_is_valid(
    action: Action,
    attempt: ExecutionAttempt,
    account: TgAccount | None,
    target: OperationTarget | None,
    payload: EnsureChannelMembershipPayload | None,
) -> bool:
    return bool(
        payload
        and account
        and target
        and action.status == "unknown_after_send"
        and attempt.status == "result_unknown"
        and attempt.gateway_call_started_at
        and action.account_id == attempt.account_id == account.id
        and action.tenant_id == account.tenant_id == target.tenant_id
        and payload.channel_id == target.tg_peer_id
        and payload.target_type == target.target_type
        and account.session_ciphertext
    )


def _confirmed_evidence(
    action: Action,
    attempt: ExecutionAttempt,
) -> RemoteReconcileEvidence:
    fact_id = typed_remote_fact_id(action, attempt, "membership_observed")
    return RemoteReconcileEvidence(
        result="remote_confirmed",
        source="membership_reprobe_read_only",
        evidence_fingerprint=canonical_state_hash({
            "action_id": action.id,
            "attempt_id": attempt.id,
            "fact_id": fact_id,
        }),
        remote_fact_id=fact_id,
        exact_match_count=1,
    )


def _inconclusive(
    case: RemoteReconcileCase,
    source: str,
) -> RemoteReconcileEvidence:
    return RemoteReconcileEvidence(
        result="inconclusive",
        source=source,
        evidence_fingerprint=canonical_state_hash({
            "case_id": case.id,
            "source": source,
        }),
    )


__all__ = ["preview_membership_probe_evidence"]
