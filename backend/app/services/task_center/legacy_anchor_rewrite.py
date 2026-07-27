from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, AiGroupMessageMemory, Task
from app.services._common import _now

from .ai_message_memory import mark_group_ai_message_result
from .daily_coverage import release_coverage_reservation


LEGACY_ANCHOR_REPLAN_CODE = "voice_profile_anchor_replan"
LEGACY_ANCHOR_REPLAN_MESSAGE = "历史账号面具强制改写正文已过期，等待重新生成"
LEGACY_ANCHOR_OPEN_STATUSES = ("pending", "claiming", "retryable_failed")


def expire_legacy_anchor_rewritten_actions(session: Session, task: Task) -> int:
    actions = session.scalars(
        select(Action).where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status.in_(LEGACY_ANCHOR_OPEN_STATUSES),
        )
    )
    expired = 0
    for action in actions:
        if not _was_anchor_rewritten(action):
            continue
        _expire_action(session, action)
        expired += 1
    if expired:
        stats = dict(task.stats or {})
        key = "voice_profile_anchor_replanned_open_action_count"
        stats[key] = int(stats.get(key) or 0) + expired
        task.stats = stats
    return expired


def reject_legacy_anchor_rewrite_before_send(
    session: Session,
    action: Action,
) -> bool:
    if not _was_anchor_rewritten(action):
        return False
    _expire_action(session, action)
    return True


def _was_anchor_rewritten(action: Action) -> bool:
    result = action.result if isinstance(action.result, dict) else {}
    return result.get("voice_profile_anchor_rewritten") is True


def _expire_action(session: Session, action: Action) -> None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    action.status = "skipped"
    action.executed_at = _now()
    action.lease_owner = ""
    action.lease_expires_at = None
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.result = {
        **dict(action.result or {}),
        "error_code": LEGACY_ANCHOR_REPLAN_CODE,
        "message": LEGACY_ANCHOR_REPLAN_MESSAGE,
    }
    _expire_memory(session, action, payload)
    _release_coverage(session, action, payload)


def _expire_memory(session: Session, action: Action, payload: dict) -> None:
    memory_id = str(payload.get("ai_message_memory_id") or "").strip()
    if not memory_id or session.get(AiGroupMessageMemory, memory_id) is None:
        return
    mark_group_ai_message_result(
        session,
        memory_id,
        status="expired_before_send",
        action_id=action.id,
        result={"error_code": LEGACY_ANCHOR_REPLAN_CODE, "action_id": action.id},
    )


def _release_coverage(session: Session, action: Action, payload: dict) -> None:
    coverage_id = str(payload.get("coverage_ledger_id") or "").strip()
    if not coverage_id:
        return
    release_coverage_reservation(
        session,
        coverage_id,
        action.id,
        blocker_code=LEGACY_ANCHOR_REPLAN_CODE,
        blocker_detail=LEGACY_ANCHOR_REPLAN_MESSAGE,
    )


__all__ = [
    "expire_legacy_anchor_rewritten_actions",
    "reject_legacy_anchor_rewrite_before_send",
]
