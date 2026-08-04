from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ExecutionAttempt,
    GatewayRequestEvidenceJournal,
    GroupBotRequiredChannelFollow,
    Task,
)
from app.models.enums import FailureType
from app.services._common import _now


REPLANNED_CODE = "group_bot_requirement_pre_gateway_replanned"
SAFE_FAILURE_TYPES = frozenset({
    FailureType.FLOOD_WAIT.value,
    FailureType.SLOWMODE.value,
})
BLOCKED_CODES = frozenset({
    "admission_version_stale",
    "group_bot_confirmation_superseded",
    REPLANNED_CODE,
})
TERMINAL_OR_RETRYABLE = frozenset({
    "failed",
    "retryable_failed",
    "pending",
    "closed_unknown",
})


def replan_group_bot_requirement_action(
    session: Session,
    action: Action,
    *,
    retry_at: datetime | None = None,
) -> Action | None:
    """Replace one requirement action only after typed pre-Gateway absence evidence."""
    if not _safe_to_replan(session, action):
        return None
    task = session.get(Task, action.task_id)
    if task is None or action.account_id is None:
        return None
    payload = dict(action.payload or {})
    if payload.get("task_group_bot_admission_id"):
        return _replan_task_action(session, action, task, payload, retry_at)
    return _replan_legacy_action(session, action, task, payload, retry_at)


def _safe_to_replan(session: Session, action: Action) -> bool:
    if action.status not in TERMINAL_OR_RETRYABLE:
        return False
    code = str((action.result or {}).get("error_code") or "")
    if code in BLOCKED_CODES:
        return False
    attempt = _latest_attempt(session, action)
    if attempt is None:
        return False
    failure_type = str(attempt.failure_type or "")
    if failure_type not in SAFE_FAILURE_TYPES:
        return False
    if attempt.gateway_call_started_at is None:
        return True
    journal = session.scalar(select(GatewayRequestEvidenceJournal).where(
        GatewayRequestEvidenceJournal.execution_attempt_id == attempt.id,
    ))
    return bool(journal and journal.remote_mutation_state == "false")


def _latest_attempt(session: Session, action: Action) -> ExecutionAttempt | None:
    return session.scalar(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.action_id == action.id)
        .order_by(ExecutionAttempt.attempt_no.desc())
        .limit(1)
    )


def _replan_legacy_action(
    session: Session,
    old_action: Action,
    task: Task,
    payload: dict,
    retry_at: datetime | None,
) -> Action | None:
    row = session.scalar(select(GroupBotRequiredChannelFollow).where(
        GroupBotRequiredChannelFollow.action_id == old_action.id,
    ))
    if row is None or row.status == "success":
        return None
    from .payloads import (
        GroupBotRequiredChannelFollowPayload,
        create_group_bot_required_channel_follow_action,
    )

    parsed = GroupBotRequiredChannelFollowPayload.model_validate(payload)
    next_payload = parsed.model_copy(
        update={"replan_attempt": int(parsed.replan_attempt or 0) + 1}
    )
    replacement = create_group_bot_required_channel_follow_action(
        session,
        task,
        int(old_action.account_id),
        retry_at or _retry_at(old_action),
        next_payload,
        flush=True,
    )
    _mark_replanned(old_action, replacement)
    row.action_id = str(replacement.id)
    row.status = "pending"
    row.failure_code = ""
    session.flush()
    return replacement


def _replan_task_action(
    session: Session,
    old_action: Action,
    task: Task,
    payload: dict,
    retry_at: datetime | None,
) -> Action | None:
    action_type = old_action.action_type
    next_payload = _increment_replan_attempt(payload)
    from .payloads import (
        GroupBotConfirmationButtonPayload,
        GroupBotRequiredChannelFollowPayload,
        create_group_bot_confirmation_button_action,
        create_group_bot_required_channel_follow_action,
    )

    if action_type == "group_bot_channel_follow":
        parsed = GroupBotRequiredChannelFollowPayload.model_validate(next_payload)
        replacement = create_group_bot_required_channel_follow_action(
            session,
            task,
            int(old_action.account_id),
            retry_at or _retry_at(old_action),
            parsed,
            flush=True,
        )
    elif action_type == "group_bot_confirmation_button":
        parsed = GroupBotConfirmationButtonPayload.model_validate(next_payload)
        replacement = create_group_bot_confirmation_button_action(
            session,
            task,
            int(old_action.account_id),
            retry_at or _retry_at(old_action),
            parsed,
            flush=True,
        )
    else:
        return None
    _mark_replanned(old_action, replacement)
    session.flush()
    return replacement


def _increment_replan_attempt(payload: dict) -> dict:
    return {
        **payload,
        "replan_attempt": int(payload.get("replan_attempt") or 0) + 1,
    }


def _retry_at(action: Action) -> datetime:
    value = str((action.result or {}).get("next_retry_at") or "")
    if value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return _now()


def _mark_replanned(old_action: Action, replacement: Action) -> None:
    old_action.status = "skipped"
    old_action.executed_at = _now()
    old_action.lease_owner = ""
    old_action.lease_expires_at = None
    old_action.claim_owner = ""
    old_action.claim_token = ""
    old_action.claim_expires_at = None
    old_action.result = {
        **dict(old_action.result or {}),
        "success": False,
        "error_code": REPLANNED_CODE,
        "replaced_action_id": str(replacement.id),
        "remote_mutation_started": False,
    }


__all__ = ["REPLANNED_CODE", "replan_group_bot_requirement_action"]
