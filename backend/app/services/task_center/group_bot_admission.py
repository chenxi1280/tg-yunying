"""Group-bot admission state machine for AI group chat."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    GroupBotAdmission,
    GroupBotAdmissionObservation,
    GroupBotAdmissionPolicy,
    GroupBotRequiredChannelFollow,
    PendingVisibilityCredit,
)
from app.models.enums import now as model_now


DEFAULT_OBSERVATION_WINDOW_SECONDS = 120
DEFAULT_VISIBILITY_WINDOW_SECONDS = 90
READY_STATE = "group_bot_admission_ready"
WAITING_STATES = {
    "awaiting_group_bot_rule",
    "observation_open",
    "group_bot_policy_unresolved",
    "required_channel_follow_pending",
    "following_required_channel",
    "awaiting_group_bot_confirmation",
    "group_bot_rule_unattributed",
    "blocked",
    "observation_stale",
    "post_send_intercepted",
    "abandoned",
    "legacy_group_bot_review",
}

CONFIRMATION_TEMPLATES = (
    "验证通过",
    "可以发言",
    "已解除禁言",
    "已通过验证",
    "验证成功",
    "已放行",
)


@dataclass(frozen=True)
class AdmissionGateDecision:
    allowed: bool
    code: str = ""
    admission_id: int | None = None
    admission_version: int | None = None
    state: str = ""


def is_group_bot_admission_ready(admission: GroupBotAdmission | None, *, canary_enforce: bool = True) -> bool:
    if not canary_enforce:
        return True
    if admission is None:
        # C1 legacy accounts may not have rows yet.
        return True
    return admission.state == READY_STATE and admission.state != "abandoned"


def get_admission(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    account_id: int,
) -> GroupBotAdmission | None:
    return session.scalar(
        select(GroupBotAdmission).where(
            GroupBotAdmission.tenant_id == tenant_id,
            GroupBotAdmission.group_id == group_id,
            GroupBotAdmission.account_id == account_id,
        )
    )


def ensure_admission_after_join(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    account_id: int,
    membership_action_id: str = "",
    join_start_cursor: str = "",
    observation_window_seconds: int = DEFAULT_OBSERVATION_WINDOW_SECONDS,
) -> GroupBotAdmission:
    existing = get_admission(session, tenant_id=tenant_id, group_id=group_id, account_id=account_id)
    now = model_now()
    closes = now + timedelta(seconds=max(60, min(300, int(observation_window_seconds or DEFAULT_OBSERVATION_WINDOW_SECONDS))))
    if existing is None:
        row = GroupBotAdmission(
            tenant_id=tenant_id,
            group_id=group_id,
            account_id=account_id,
            membership_action_id=str(membership_action_id or ""),
            state="awaiting_group_bot_rule",
            join_start_cursor=str(join_start_cursor or ""),
            join_success_at=now,
            observation_closes_at=closes,
            admission_version=1,
            transport_observation={},
            required_channel_refs=[],
        )
        session.add(row)
        session.flush()
        return row

    # Rejoin / new membership generation resets observation.
    if membership_action_id and existing.membership_action_id != str(membership_action_id):
        existing.membership_action_id = str(membership_action_id)
        existing.state = "awaiting_group_bot_rule"
        existing.admission_version = int(existing.admission_version or 1) + 1
        existing.join_start_cursor = str(join_start_cursor or existing.join_start_cursor or "")
        existing.observed_end_cursor = ""
        existing.trusted_bot_peer_id = ""
        existing.required_channel_refs = []
        existing.failure_code = ""
        existing.join_success_at = now
        existing.observation_closes_at = closes
        existing.post_send_visibility_state = ""
        session.flush()
    return existing


def active_policy(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    completion_policy: str,
    trusted_bot_peer_id: str = "",
) -> GroupBotAdmissionPolicy | None:
    stmt = select(GroupBotAdmissionPolicy).where(
        GroupBotAdmissionPolicy.tenant_id == tenant_id,
        GroupBotAdmissionPolicy.group_id == group_id,
        GroupBotAdmissionPolicy.completion_policy == completion_policy,
        GroupBotAdmissionPolicy.status == "active",
    )
    if completion_policy == "follow_sufficient":
        stmt = stmt.where(GroupBotAdmissionPolicy.trusted_bot_peer_id == str(trusted_bot_peer_id or ""))
    return session.scalar(stmt.order_by(GroupBotAdmissionPolicy.policy_version.desc()).limit(1))


def create_policy(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    completion_policy: str,
    reason: str,
    evidence_ref: str,
    created_by: str,
    trusted_bot_peer_id: str = "",
    expected_policy_version: int | None = None,
) -> GroupBotAdmissionPolicy:
    if completion_policy not in {"not_required", "follow_sufficient", "explicit_bot_confirmation"}:
        raise ValueError("unsupported completion_policy")
    if completion_policy == "follow_sufficient" and not str(trusted_bot_peer_id or "").strip():
        raise ValueError("follow_sufficient requires trusted_bot_peer_id")
    # Active uniqueness: one not_required per group; one follow_sufficient per group+bot.
    current = active_policy(
        session,
        tenant_id=tenant_id,
        group_id=group_id,
        completion_policy=completion_policy,
        trusted_bot_peer_id=trusted_bot_peer_id,
    )
    current_version = int(current.policy_version or 1) if current else 0
    if expected_policy_version is not None and int(expected_policy_version) != current_version:
        raise ValueError("policy_version_conflict")
    if current is not None:
        current.status = "revoked"
        current.revoked_by = created_by
        current.revoked_at = model_now()
    row = GroupBotAdmissionPolicy(
        tenant_id=tenant_id,
        group_id=group_id,
        trusted_bot_peer_id=str(trusted_bot_peer_id or ""),
        completion_policy=completion_policy,
        evidence_ref=str(evidence_ref or ""),
        reason=str(reason or ""),
        policy_version=current_version + 1,
        status="active",
        created_by=created_by,
        effective_at=model_now(),
    )
    session.add(row)
    session.flush()
    return row


def abandon_admission(
    session: Session,
    *,
    admission: GroupBotAdmission,
    reason: str,
    evidence_ref: str,
    abandoned_by: str,
    expected_admission_version: int,
) -> GroupBotAdmission:
    if int(admission.admission_version or 1) != int(expected_admission_version):
        raise ValueError("admission_version_conflict")
    admission.state = "abandoned"
    admission.abandoned_reason = str(reason or "")
    admission.evidence_ref = str(evidence_ref or admission.evidence_ref or "")
    admission.abandoned_by = str(abandoned_by or "")
    admission.abandoned_at = model_now()
    admission.failure_code = "admission_abandoned"
    session.flush()
    return admission


def record_observation_batch(
    session: Session,
    *,
    admission: GroupBotAdmission,
    observed_end_cursor: str,
    listener_account_id: int | None = None,
    read_count: int = 0,
    cursor_gap: bool = False,
    failure_code: str = "",
    result_summary: dict[str, Any] | None = None,
) -> GroupBotAdmissionObservation:
    obs = GroupBotAdmissionObservation(
        admission_id=admission.id,
        join_start_cursor=str(admission.join_start_cursor or ""),
        observed_end_cursor=str(observed_end_cursor or ""),
        listener_account_id=listener_account_id,
        read_count=int(read_count or 0),
        cursor_gap=bool(cursor_gap),
        failure_code=str(failure_code or ""),
        observation_version=int(admission.admission_version or 1),
        result_summary=dict(result_summary or {}),
    )
    session.add(obs)
    if not cursor_gap and not failure_code:
        admission.observed_end_cursor = str(observed_end_cursor or admission.observed_end_cursor or "")
    elif cursor_gap:
        admission.state = "observation_stale"
        admission.failure_code = failure_code or "cursor_gap"
    session.flush()
    return obs


def close_observation_if_due(
    session: Session,
    *,
    admission: GroupBotAdmission,
    now: datetime | None = None,
) -> GroupBotAdmission:
    if admission.state not in {"awaiting_group_bot_rule", "observation_open", "observation_stale"}:
        return admission
    if admission.failure_code == "cursor_gap" or admission.state == "observation_stale":
        return admission
    current = now or model_now()
    closes_at = admission.observation_closes_at
    if closes_at is not None and current < closes_at:
        admission.state = "observation_open"
        session.flush()
        return admission
    # Observation window closed with continuous cursor and no trusted rule.
    policy = active_policy(
        session,
        tenant_id=admission.tenant_id,
        group_id=admission.group_id,
        completion_policy="not_required",
    )
    if policy is not None:
        admission.state = "group_bot_rule_clear"
        admission.completion_policy = "not_required"
        admission.policy_version = int(policy.policy_version or 1)
        # Ready only after can_send is independently true; mark admission ready for gate.
        admission.state = READY_STATE
        admission.failure_code = ""
    else:
        admission.state = "group_bot_policy_unresolved"
        admission.failure_code = "group_bot_policy_unresolved"
    session.flush()
    return admission


def parse_channel_refs(text: str) -> list[str]:
    refs: list[str] = []
    for match in re.finditer(r"@([A-Za-z][A-Za-z0-9_]{3,})", text or ""):
        username = match.group(1)
        if username.lower().endswith("bot"):
            continue
        refs.append(username)
    for match in re.finditer(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,})", text or "", flags=re.I):
        username = match.group(1)
        if username.lower() in {"joinchat", "addstickers"}:
            continue
        refs.append(username)
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for item in refs:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def attribute_prompt_to_account(
    *,
    text: str,
    waiting_account_ids: list[int],
    account_usernames: dict[int, str],
    account_display_names: dict[int, str],
    explicit_account_id: int | None = None,
) -> tuple[int | None, str]:
    if explicit_account_id and explicit_account_id in waiting_account_ids:
        return int(explicit_account_id), "explicit"
    text_l = (text or "").lower()
    hits: list[int] = []
    for account_id in waiting_account_ids:
        username = str(account_usernames.get(account_id) or "").strip().lstrip("@").lower()
        display = str(account_display_names.get(account_id) or "").strip().lower()
        if username and (f"@{username}" in text_l or username in text_l):
            hits.append(account_id)
            continue
        if display and display in text_l:
            hits.append(account_id)
    hits = list(dict.fromkeys(hits))
    if len(hits) == 1:
        return hits[0], "text_match"
    if len(waiting_account_ids) == 1:
        return int(waiting_account_ids[0]), "unique_waiting"
    return None, "unattributed"


def ingest_trusted_bot_prompt(
    session: Session,
    *,
    admission: GroupBotAdmission,
    message_id: str,
    text: str,
    bot_peer_id: str,
    is_admin_bot: bool,
    bound_task_id: str = "",
) -> GroupBotAdmission:
    if not is_admin_bot:
        admission.failure_code = "untrusted_bot_source"
        session.flush()
        return admission
    peer = str(bot_peer_id or "").strip()
    if admission.trusted_bot_peer_id and admission.trusted_bot_peer_id != peer:
        admission.state = "blocked"
        admission.failure_code = "group_bot_multi_bot_conflict"
        session.flush()
        return admission
    if not admission.trusted_bot_peer_id:
        admission.trusted_bot_peer_id = peer
    refs = parse_channel_refs(text)
    admission.source_message_id = str(message_id or "")
    admission.required_channel_refs = refs
    if not refs:
        # Button-only / confirmation-only path.
        admission.state = "awaiting_group_bot_confirmation"
        admission.completion_policy = admission.completion_policy or "explicit_bot_confirmation"
        session.flush()
        return admission
    admission.state = "required_channel_follow_pending"
    for ref in refs:
        existing = session.scalar(
            select(GroupBotRequiredChannelFollow).where(
                GroupBotRequiredChannelFollow.admission_id == admission.id,
                GroupBotRequiredChannelFollow.channel_ref == ref,
            )
        )
        if existing is None:
            session.add(
                GroupBotRequiredChannelFollow(
                    admission_id=admission.id,
                    channel_ref=ref,
                    source_message_id=str(message_id or ""),
                    status="pending",
                )
            )
    session.flush()
    if bound_task_id:
        plan_required_channel_follow_actions(
            session,
            admission=admission,
            task_id=str(bound_task_id),
            source_message_id=str(message_id or ""),
        )
    return admission


def plan_required_channel_follow_actions(
    session: Session,
    *,
    admission: GroupBotAdmission,
    task_id: str,
    source_message_id: str = "",
) -> list[Any]:
    """Create bound group_bot_required_channel_follow Actions for pending refs."""
    from app.models import Task
    from app.services._common import _now
    from app.services.task_center.payloads import (
        GroupBotRequiredChannelFollowPayload,
        create_group_bot_required_channel_follow_action,
    )

    task = session.get(Task, task_id)
    if task is None or task.tenant_id != admission.tenant_id:
        return []
    pending_refs = session.scalars(
        select(GroupBotRequiredChannelFollow).where(
            GroupBotRequiredChannelFollow.admission_id == admission.id,
            GroupBotRequiredChannelFollow.status == "pending",
        )
    ).all()
    created: list[Any] = []
    now = _now()
    for row in pending_refs:
        if row.action_id:
            continue
        payload = GroupBotRequiredChannelFollowPayload(
            group_id=int(admission.group_id),
            admission_id=int(admission.id),
            admission_version=int(admission.admission_version or 1),
            channel_ref=str(row.channel_ref),
            source_message_id=str(source_message_id or row.source_message_id or ""),
            admission_bound_task_id=str(task.id),
            admission_bound_account_id=int(admission.account_id),
        )
        action = create_group_bot_required_channel_follow_action(
            session,
            task,
            int(admission.account_id),
            now,
            payload,
            flush=True,
        )
        row.action_id = str(action.id)
        created.append(action)
    session.flush()
    return created


def resolve_bound_task_id_for_group(session: Session, *, tenant_id: int, group_id: int) -> str:
    """Pick a running group_ai_chat task for this group to bind follow actions."""
    from app.models import Task

    task = session.scalar(
        select(Task)
        .where(
            Task.tenant_id == tenant_id,
            Task.type == "group_ai_chat",
            Task.status.in_(("draft", "pending", "running", "paused")),
            Task.deleted_at.is_(None),
            Task.type_config["target_group_id"].as_integer() == int(group_id),
        )
        .order_by(Task.updated_at.desc(), Task.id.desc())
        .limit(1)
    )
    return str(task.id) if task is not None else ""


def mark_channel_follow_completed(
    session: Session,
    *,
    admission: GroupBotAdmission,
    channel_ref: str,
    resolved_peer_id: str = "",
    resolved_type: str = "broadcast",
    action_id: str = "",
) -> GroupBotAdmission:
    row = session.scalar(
        select(GroupBotRequiredChannelFollow).where(
            GroupBotRequiredChannelFollow.admission_id == admission.id,
            GroupBotRequiredChannelFollow.channel_ref == channel_ref,
        )
    )
    if row is None:
        raise ValueError("required_channel_ref_not_found")
    row.status = "success"
    row.resolved_peer_id = str(resolved_peer_id or "")
    row.resolved_type = str(resolved_type or "broadcast")
    row.action_id = str(action_id or row.action_id or "")
    row.completed_at = model_now()
    session.flush()
    pending = session.scalars(
        select(GroupBotRequiredChannelFollow).where(
            GroupBotRequiredChannelFollow.admission_id == admission.id,
            GroupBotRequiredChannelFollow.status != "success",
        )
    ).all()
    if pending:
        admission.state = "following_required_channel"
        session.flush()
        return admission
    follow_policy = active_policy(
        session,
        tenant_id=admission.tenant_id,
        group_id=admission.group_id,
        completion_policy="follow_sufficient",
        trusted_bot_peer_id=admission.trusted_bot_peer_id,
    )
    if follow_policy is not None:
        admission.state = READY_STATE
        admission.completion_policy = "follow_sufficient"
        admission.policy_version = int(follow_policy.policy_version or 1)
        admission.failure_code = ""
    else:
        admission.state = "awaiting_group_bot_confirmation"
        admission.completion_policy = "explicit_bot_confirmation"
    session.flush()
    return admission


def apply_confirmation_event(
    session: Session,
    *,
    admission: GroupBotAdmission,
    message_id: str,
    text: str,
    bot_peer_id: str,
    button_confirmed: bool = False,
) -> GroupBotAdmission:
    if admission.state not in {"awaiting_group_bot_confirmation", "following_required_channel", READY_STATE}:
        return admission
    peer = str(bot_peer_id or "").strip()
    if admission.trusted_bot_peer_id and peer and admission.trusted_bot_peer_id != peer:
        admission.failure_code = "group_bot_multi_bot_conflict"
        admission.state = "blocked"
        session.flush()
        return admission
    text_ok = any(token in (text or "") for token in CONFIRMATION_TEMPLATES)
    if not (button_confirmed or text_ok):
        return admission
    admission.confirmation_message_id = str(message_id or "")
    admission.state = READY_STATE
    admission.completion_policy = admission.completion_policy or "explicit_bot_confirmation"
    admission.failure_code = ""
    session.flush()
    return admission


def record_probe_observation(admission: GroupBotAdmission, probe: dict[str, Any]) -> None:
    """Transport probe only; never promotes ready."""
    observation = dict(admission.transport_observation or {})
    observation["last_probe"] = dict(probe or {})
    observation["updated_at"] = model_now().isoformat()
    admission.transport_observation = observation


def evaluate_send_gate(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    account_id: int,
    enforce: bool = True,
) -> AdmissionGateDecision:
    admission = get_admission(session, tenant_id=tenant_id, group_id=group_id, account_id=account_id)
    if not enforce:
        return AdmissionGateDecision(True, code="legacy_send_until_reviewed", state=admission.state if admission else "")
    if admission is None:
        # C1 canary: stock accounts without admission rows keep legacy send path.
        return AdmissionGateDecision(True, code="legacy_send_until_reviewed", state="missing")
    if admission.state == "abandoned":
        return AdmissionGateDecision(
            False,
            code="admission_abandoned",
            admission_id=admission.id,
            admission_version=int(admission.admission_version or 1),
            state=admission.state,
        )
    if admission.state != READY_STATE:
        return AdmissionGateDecision(
            False,
            code="group_bot_admission_wait",
            admission_id=admission.id,
            admission_version=int(admission.admission_version or 1),
            state=admission.state,
        )
    return AdmissionGateDecision(
        True,
        code="ready",
        admission_id=admission.id,
        admission_version=int(admission.admission_version or 1),
        state=admission.state,
    )


def needs_post_send_visibility(admission: GroupBotAdmission | None, *, action_admission_version: int | None) -> bool:
    if admission is None:
        return False
    if admission.state != READY_STATE:
        return False
    if admission.post_send_visibility_state in {"", "required", "pending"}:
        # First message after ready / version change.
        if action_admission_version is None:
            return True
        return int(action_admission_version) == int(admission.admission_version or 1) and admission.post_send_visibility_state != "visible_confirmed"
    return False


def open_pending_visibility_credit(
    session: Session,
    *,
    tenant_id: int,
    action_id: str,
    remote_message_id: str,
    execution_attempt_id: str | None = None,
    bucket_id: int | None = None,
) -> PendingVisibilityCredit:
    existing = session.scalar(select(PendingVisibilityCredit).where(PendingVisibilityCredit.action_id == action_id))
    if existing is not None:
        return existing
    row = PendingVisibilityCredit(
        tenant_id=tenant_id,
        action_id=action_id,
        bucket_id=bucket_id,
        execution_attempt_id=execution_attempt_id,
        remote_message_id=str(remote_message_id or ""),
        hold_reason="pending_visibility",
        status="open",
    )
    session.add(row)
    session.flush()
    return row


def close_pending_visibility_credit(
    session: Session,
    *,
    action_id: str,
    status: str,
) -> PendingVisibilityCredit | None:
    row = session.scalar(select(PendingVisibilityCredit).where(PendingVisibilityCredit.action_id == action_id))
    if row is None:
        return None
    row.status = status
    row.closed_at = model_now()
    session.flush()
    return row


def mark_post_send_intercepted(session: Session, *, admission: GroupBotAdmission) -> None:
    admission.state = "post_send_intercepted"
    admission.post_send_visibility_state = "post_send_intercepted"
    admission.failure_code = "post_send_intercepted"
    admission.admission_version = int(admission.admission_version or 1) + 1
    session.flush()


def mark_visible_confirmed(session: Session, *, admission: GroupBotAdmission) -> None:
    admission.post_send_visibility_state = "visible_confirmed"
    session.flush()


def reconcile_unresolved_with_not_required(session: Session, *, tenant_id: int, group_id: int) -> int:
    policy = active_policy(
        session,
        tenant_id=tenant_id,
        group_id=group_id,
        completion_policy="not_required",
    )
    if policy is None:
        return 0
    rows = session.scalars(
        select(GroupBotAdmission).where(
            GroupBotAdmission.tenant_id == tenant_id,
            GroupBotAdmission.group_id == group_id,
            GroupBotAdmission.state == "group_bot_policy_unresolved",
        )
    ).all()
    count = 0
    for row in rows:
        row.state = READY_STATE
        row.completion_policy = "not_required"
        row.policy_version = int(policy.policy_version or 1)
        row.failure_code = ""
        count += 1
    session.flush()
    return count


def reopen_admission(
    session: Session,
    *,
    admission: GroupBotAdmission,
    expected_admission_version: int,
    reopened_by: str = "",
) -> GroupBotAdmission:
    """PRD §5.8.2 #6: reopen is the only recovery path from abandoned."""
    if admission.state != "abandoned":
        raise ValueError("admission_not_abandoned")
    if int(admission.admission_version or 1) != int(expected_admission_version):
        raise ValueError("admission_version_conflict")
    admission.state = "awaiting_group_bot_rule"
    admission.failure_code = ""
    admission.abandoned_reason = ""
    admission.post_send_visibility_state = ""
    admission.admission_version = int(admission.admission_version or 1) + 1
    admission.observation_closes_at = model_now() + timedelta(seconds=DEFAULT_OBSERVATION_WINDOW_SECONDS)
    session.flush()
    return admission


def revoke_policy(
    session: Session,
    *,
    policy: GroupBotAdmissionPolicy,
    revoked_by: str,
    expected_policy_version: int,
) -> GroupBotAdmissionPolicy:
    """Explicit revoke of an active policy."""
    if int(policy.policy_version or 1) != int(expected_policy_version):
        raise ValueError("policy_version_conflict")
    if policy.status != "active":
        raise ValueError("policy_not_active")
    policy.status = "revoked"
    policy.revoked_by = str(revoked_by or "")
    policy.revoked_at = model_now()
    session.flush()
    return policy


__all__ = [
    "AdmissionGateDecision",
    "DEFAULT_OBSERVATION_WINDOW_SECONDS",
    "DEFAULT_VISIBILITY_WINDOW_SECONDS",
    "READY_STATE",
    "WAITING_STATES",
    "is_group_bot_admission_ready",
    "get_admission",
    "ensure_admission_after_join",
    "active_policy",
    "create_policy",
    "abandon_admission",
    "reopen_admission",
    "revoke_policy",
    "record_observation_batch",
    "close_observation_if_due",
    "parse_channel_refs",
    "attribute_prompt_to_account",
    "ingest_trusted_bot_prompt",
    "plan_required_channel_follow_actions",
    "resolve_bound_task_id_for_group",
    "mark_channel_follow_completed",
    "apply_confirmation_event",
    "record_probe_observation",
    "evaluate_send_gate",
    "needs_post_send_visibility",
    "open_pending_visibility_credit",
    "close_pending_visibility_credit",
    "mark_post_send_intercepted",
    "mark_visible_confirmed",
    "reconcile_unresolved_with_not_required",
]
