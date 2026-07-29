from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, Task
from .fulfillment_takeover import FULFILLMENT_CONTRACT_VERSION


DEFAULT_AUTO_RETRY_STATUSES = ("failed", "retryable_failed")
TARGET_ADMISSION_AUTO_RETRY_STATUSES = ("failed", "retryable_failed")
TARGET_ADMISSION_DEFAULT_MAX_RETRIES = 1
TARGET_ADMISSION_DEFAULT_RETRY_DELAY_SECONDS = 30
AI_GROUP_TERMINAL_QUALITY_ERRORS = frozenset(
    {"duplicate_message", "ai_message_memory_missing"}
)
AI_GROUP_TERMINAL_GENERATION_STATUSES = frozenset({"duplicate_rejected"})


def retry_failed_actions(
    session: Session,
    task: Task,
    *,
    limit: int = 100,
    now_value: datetime,
) -> int:
    policy = task.failure_policy or {}
    max_retries = _max_retries_for_task(task, policy)
    if max_retries <= 0:
        return 0
    retry_delay = _retry_delay_seconds_for_task(task, policy)
    backoff = policy.get("retry_backoff") or "none"
    actions = session.scalars(_retry_query(task, max_retries, limit))
    count = 0
    for action in actions:
        if _action_retry_is_blocked(task, action):
            continue
        _schedule_retry(
            action,
            retry_delay=retry_delay,
            backoff=backoff,
            now_value=now_value,
        )
        count += 1
    return count


def _retry_query(task: Task, max_retries: int, limit: int):
    return (
        select(Action)
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.status.in_(_auto_retry_statuses(task)),
            Action.retry_count < max_retries,
        )
        .order_by(Action.scheduled_at.asc(), Action.id.asc())
        .limit(max(1, int(limit)))
    )


def _action_retry_is_blocked(task: Task, action: Action) -> bool:
    if _is_unbound_legacy_fulfillment_action(task, action):
        return True
    if _is_bound_search_click_action(task, action):
        return True
    return _is_terminal_ai_quality_failure(
        action,
        dict(action.result or {}),
    )


def _schedule_retry(
    action: Action,
    *,
    retry_delay: int,
    backoff: str,
    now_value: datetime,
) -> None:
    previous_result = dict(action.result or {})
    action.retry_count += 1
    delay = _retry_delay_with_backoff(
        retry_delay,
        action.retry_count,
        backoff,
    )
    action.status = "pending"
    action.scheduled_at = now_value + timedelta(seconds=delay)
    action.executed_at = None
    action.lease_owner = ""
    action.lease_expires_at = None
    action.result = {
        "retry_scheduled": True,
        "retry_count": int(action.retry_count or 0),
        "retry_after_seconds": max(0, int(delay)),
        "last_failure": previous_result,
    }


def _retry_delay_with_backoff(
    retry_delay: int,
    retry_count: int,
    backoff: str,
) -> int:
    if backoff == "linear":
        return retry_delay * retry_count
    if backoff == "exponential":
        return retry_delay * 2 ** max(0, retry_count - 1)
    return retry_delay


def _is_unbound_legacy_fulfillment_action(
    task: Task,
    action: Action,
) -> bool:
    if not _uses_current_fulfillment_contract(task):
        return False
    if task.type == "group_ai_chat" and action.action_type == "send_message":
        return not action.primary_quantity_slot_id
    if task.type != "search_click" or action.action_type != "search_join":
        return False
    payload = action.payload if isinstance(action.payload, dict) else {}
    return not str(payload.get("search_click_obligation_id") or "")


def _is_bound_search_click_action(task: Task, action: Action) -> bool:
    if (
        task.type != "search_click"
        or action.action_type != "search_join"
        or not _uses_current_fulfillment_contract(task)
    ):
        return False
    payload = action.payload if isinstance(action.payload, dict) else {}
    return bool(
        payload.get("search_click_obligation_id")
        and payload.get("search_click_assignment_id")
    )


def _uses_current_fulfillment_contract(task: Task) -> bool:
    return (
        (task.stats or {}).get("fulfillment_contract_version")
        == FULFILLMENT_CONTRACT_VERSION
    )


def _is_terminal_ai_quality_failure(
    action: Action,
    previous_result: dict[str, Any],
) -> bool:
    if action.task_type != "group_ai_chat" or action.action_type != "send_message":
        return False
    payload = action.payload if isinstance(action.payload, dict) else {}
    error_code = str(
        previous_result.get("error_code")
        or previous_result.get("failure_type")
        or ""
    )
    generation_status = str(payload.get("ai_generation_status") or "")
    quality_reason = str(
        payload.get("quality_skip_reason")
        or previous_result.get("quality_skip_reason")
        or ""
    )
    return (
        error_code in AI_GROUP_TERMINAL_QUALITY_ERRORS
        or generation_status in AI_GROUP_TERMINAL_GENERATION_STATUSES
        or quality_reason in AI_GROUP_TERMINAL_QUALITY_ERRORS
    )


def _auto_retry_statuses(task: Task) -> tuple[str, ...]:
    if task.type == "target_admission_retry":
        return TARGET_ADMISSION_AUTO_RETRY_STATUSES
    return DEFAULT_AUTO_RETRY_STATUSES


def _max_retries_for_task(task: Task, policy: dict[str, Any]) -> int:
    if policy.get("max_retries") is not None:
        return int(policy.get("max_retries") or 0)
    if task.type == "target_admission_retry":
        return TARGET_ADMISSION_DEFAULT_MAX_RETRIES
    return 0


def _retry_delay_seconds_for_task(task: Task, policy: dict[str, Any]) -> int:
    if policy.get("retry_delay_seconds") is not None:
        return int(policy["retry_delay_seconds"])
    if task.type == "target_admission_retry":
        return TARGET_ADMISSION_DEFAULT_RETRY_DELAY_SECONDS
    return 60


__all__ = ["retry_failed_actions"]
