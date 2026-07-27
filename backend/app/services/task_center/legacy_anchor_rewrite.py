from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, AiGroupMessageMemory, Task
from app.services._common import _now

from .ai_message_memory import mark_group_ai_message_result
from .daily_coverage import release_coverage_reservation


LEGACY_ANCHOR_REPLAN_CODE = "voice_profile_anchor_replan"
LEGACY_ANCHOR_REPLAN_MESSAGE = "历史账号面具消费者合同正文已过期，等待重新生成"
LEGACY_ANCHOR_OPEN_STATUSES = ("pending", "claiming", "retryable_failed")
VOICE_PROFILE_CONTRACT_VERSION = "style_only_v2"
DAILY_CONTENT_CONTRACT_REPLAN_CODE = "daily_content_contract_replan"
DAILY_CONTENT_CONTRACT_REPLAN_MESSAGE = (
    "日覆盖旧规划缺少当前账号面具或签到兜底证据，等待重新生成"
)
DIRECT_CHECK_IN_GENERATION_SOURCES = frozenset({
    "direct_check_in",
    "mask_missing_check_in",
})


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
        if not _requires_contract_replan(action):
            continue
        _expire_action(session, action)
        expired += 1
    if expired:
        stats = dict(task.stats or {})
        key = "voice_profile_anchor_replanned_open_action_count"
        stats[key] = int(stats.get(key) or 0) + expired
        task.stats = stats
    return expired


def expire_incomplete_daily_contract_actions(session: Session, task: Task) -> int:
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
        if _has_current_daily_content_contract(action):
            continue
        _expire_action_with_reason(
            session,
            action,
            error_code=DAILY_CONTENT_CONTRACT_REPLAN_CODE,
            message=DAILY_CONTENT_CONTRACT_REPLAN_MESSAGE,
        )
        expired += 1
    if expired:
        stats = dict(task.stats or {})
        key = "daily_content_contract_replanned_open_action_count"
        stats[key] = int(stats.get(key) or 0) + expired
        task.stats = stats
    return expired


def _has_current_daily_content_contract(action: Action) -> bool:
    payload = action.payload if isinstance(action.payload, dict) else {}
    if not str(payload.get("daily_group_target_id") or "").strip():
        return False
    source = str(payload.get("content_source") or "").strip()
    if source == "account_mask":
        return bool(
            str(payload.get("account_mask_id") or "").strip()
            and int(payload.get("account_mask_version") or 0) > 0
            and str(payload.get("account_mask_snapshot_hash") or "").strip()
            and payload.get("mask_status") == "active"
        )
    if source != "mask_missing_check_in":
        return False
    return bool(
        str(payload.get("coverage_ledger_id") or "").strip()
        and str(payload.get("fallback_obligation_key") or "").strip()
        and payload.get("mask_status") == "missing"
    )


def reject_legacy_anchor_rewrite_before_send(
    session: Session,
    action: Action,
) -> bool:
    if not _requires_contract_replan(action):
        return False
    _expire_action(session, action)
    return True


def _was_anchor_rewritten(action: Action) -> bool:
    result = action.result if isinstance(action.result, dict) else {}
    return result.get("voice_profile_anchor_rewritten") is True


def _requires_contract_replan(action: Action) -> bool:
    if _was_anchor_rewritten(action):
        return True
    payload = action.payload if isinstance(action.payload, dict) else {}
    if payload.get("generation_source") in DIRECT_CHECK_IN_GENERATION_SOURCES:
        return False
    generated = (
        payload.get("ai_generation_status") == "ready"
        and bool(str(payload.get("message_text") or "").strip())
    )
    return generated and payload.get("voice_profile_contract_version") != VOICE_PROFILE_CONTRACT_VERSION


def _expire_action(session: Session, action: Action) -> None:
    _expire_action_with_reason(
        session,
        action,
        error_code=LEGACY_ANCHOR_REPLAN_CODE,
        message=LEGACY_ANCHOR_REPLAN_MESSAGE,
    )


def _expire_action_with_reason(
    session: Session,
    action: Action,
    *,
    error_code: str,
    message: str,
) -> None:
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
        "error_code": error_code,
        "message": message,
    }
    _expire_memory(session, action, payload, error_code=error_code)
    _release_coverage(session, action, payload, error_code=error_code, message=message)


def _expire_memory(session: Session, action: Action, payload: dict, *, error_code: str) -> None:
    memory_id = str(payload.get("ai_message_memory_id") or "").strip()
    if not memory_id or session.get(AiGroupMessageMemory, memory_id) is None:
        return
    mark_group_ai_message_result(
        session,
        memory_id,
        status="expired_before_send",
        action_id=action.id,
        result={"error_code": error_code, "action_id": action.id},
    )


def _release_coverage(
    session: Session,
    action: Action,
    payload: dict,
    *,
    error_code: str,
    message: str,
) -> None:
    coverage_id = str(payload.get("coverage_ledger_id") or "").strip()
    if not coverage_id:
        return
    release_coverage_reservation(
        session,
        coverage_id,
        action.id,
        blocker_code=error_code,
        blocker_detail=message,
    )


__all__ = [
    "VOICE_PROFILE_CONTRACT_VERSION",
    "expire_incomplete_daily_contract_actions",
    "expire_legacy_anchor_rewritten_actions",
    "reject_legacy_anchor_rewrite_before_send",
]
