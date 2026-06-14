from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Action, Task
from app.services._common import _now
from app.timezone import BEIJING_TZ, as_beijing

from .config_fields import CHANNEL_DYNAMIC_TASK_TYPES
from .hard_hourly import enabled as hard_hourly_enabled, hard_hourly_stats
from .pacing import ai_next_run_after, next_run_after

ARCHIVED_SKIP_ERROR_CODES = {"context_expired"}
BUSINESS_MEMBERSHIP_ACTION_TYPES = ["ensure_channel_membership", "ensure_target_membership"]
PLANNER_BACKLOG_OPEN_STATUSES = {"pending", "claiming", "executing"}
PLANNER_BACKLOG_STAT_KEYS = (
    "planner_backlog_blocked",
    "planner_backlog_blocked_at",
    "planner_backlog_global_pending",
    "planner_backlog_task_pending",
    "planner_backlog_oldest_age_seconds",
)
HARD_HOURLY_EXPIRED_ERROR_CODE = "hard_hourly_bucket_expired"
HARD_HOURLY_EXPIRED_ERROR_MESSAGE = "硬目标小时窗口已结束，过期补量已跳过"


def next_run_after_task(task: Task):
    config = task.type_config or {}
    if task.type == "group_ai_chat":
        hard_next = _stats_datetime(task, "hard_hourly_next_check_at")
        if hard_hourly_enabled(task) and hard_next:
            return max(hard_next, _now())
        waiting_until = _stats_datetime(task, "idle_continuation_next_run_at")
        if waiting_until:
            return waiting_until
        return ai_next_run_after(task.pacing_config or {})
    if task.type in CHANNEL_DYNAMIC_TASK_TYPES and (config.get("message_scope") or "latest_n") == "dynamic_new":
        interval = int(config.get("listener_interval_seconds") or 30)
        return utc_now_naive() + timedelta(seconds=max(1, interval))
    return next_run_after(task.pacing_config or {})


def refresh_task_stats(session: Session, task: Task) -> dict[str, Any]:
    session.flush()
    business_filter = Action.action_type.notin_(BUSINESS_MEMBERSHIP_ACTION_TYPES)
    rows = session.execute(select(Action.status, func.count(Action.id)).where(Action.task_id == task.id, business_filter).group_by(Action.status)).all()
    counts = {str(status): int(count) for status, count in rows}
    raw_skipped_count = counts.get("skipped", 0)
    archived_skipped_count = _archived_skipped_count(session, task, business_filter)
    skipped_count = max(0, raw_skipped_count - archived_skipped_count)
    accounts_used = session.scalar(select(func.count(func.distinct(Action.account_id))).where(Action.task_id == task.id, business_filter, Action.account_id.is_not(None))) or 0
    last_action_at = session.scalar(select(func.max(Action.executed_at)).where(Action.task_id == task.id, business_filter))
    stats = dict(task.stats or empty_stats())
    stats = _clear_recovered_planner_backlog_stats(session, task, stats)
    stats.update(
        {
            "total_actions": max(0, sum(counts.values()) - archived_skipped_count),
            "success_count": counts.get("success", 0),
            "failure_count": counts.get("failed", 0),
            "pending_count": counts.get("pending", 0),
            "claiming_count": counts.get("claiming", 0),
            "executing_count": counts.get("executing", 0),
            "retryable_failed_count": counts.get("retryable_failed", 0),
            "unknown_after_send_count": counts.get("unknown_after_send", 0),
            "skipped_count": skipped_count,
            "raw_skipped_count": raw_skipped_count,
            "archived_skipped_count": archived_skipped_count,
            "accounts_used": int(accounts_used or 0),
            "last_action_at": last_action_at.isoformat() if last_action_at else stats.get("last_action_at"),
        }
    )
    stats = hard_hourly_stats(session, task, _now(), stats)
    task.stats = stats
    from app.services.runtime_summary import refresh_task_summary

    refresh_task_summary(session, task)
    return stats


def planner_backlog_snapshot(session: Session, task: Task) -> dict[str, int | bool]:
    settings = get_settings()
    task_filters = [
        Action.task_id == task.id,
        Action.status.in_(PLANNER_BACKLOG_OPEN_STATUSES),
    ]
    if _can_plan_with_partial_membership(task):
        task_filters.append(Action.action_type.notin_(BUSINESS_MEMBERSHIP_ACTION_TYPES))
    now_value = _now()
    global_actions = _active_backlog_actions(session, [Action.status.in_(PLANNER_BACKLOG_OPEN_STATUSES)], now_value)
    task_actions = _active_backlog_actions(session, task_filters, now_value)
    global_pending = len(global_actions)
    task_pending = len(task_actions)
    oldest_pending = min((action.scheduled_at for action in task_actions if action.scheduled_at), default=None)
    oldest_at = as_beijing(oldest_pending)
    oldest_age = int((_now() - oldest_at).total_seconds()) if oldest_at else 0
    blocked = (
        int(global_pending or 0) >= int(settings.max_pending_global or 0)
        or int(task_pending or 0) >= int(settings.max_pending_per_task or 0)
        or oldest_age >= int(settings.oldest_pending_age_seconds or 0)
    )
    return {
        "blocked": blocked,
        "global_pending": int(global_pending or 0),
        "task_pending": int(task_pending or 0),
        "oldest_age_seconds": int(oldest_age),
    }


def _active_backlog_actions(session: Session, filters: list[Any], now_value: datetime) -> list[Action]:
    actions = session.scalars(select(Action).where(*filters)).all()
    return [action for action in actions if not _hard_hourly_bucket_expired(action, now_value)]


def _can_plan_with_partial_membership(task: Task) -> bool:
    stats = task.stats if isinstance(task.stats, dict) else {}
    raw_blockers = stats.get("hard_hourly_last_blockers")
    blockers = raw_blockers if isinstance(raw_blockers, dict) else {}
    return (
        task.type == "group_ai_chat"
        and hard_hourly_enabled(task)
        and int(stats.get("membership_joined_count") or 0) > 0
        and int(blockers.get("target_membership_pending") or 0) > 0
    )


def clear_planner_backlog_stats(stats: dict[str, Any]) -> dict[str, Any]:
    updated = dict(stats or {})
    for key in PLANNER_BACKLOG_STAT_KEYS:
        updated.pop(key, None)
    return updated


def _clear_recovered_planner_backlog_stats(session: Session, task: Task, stats: dict[str, Any]) -> dict[str, Any]:
    if not stats.get("planner_backlog_blocked"):
        return stats
    if planner_backlog_snapshot(session, task)["blocked"]:
        return stats
    return clear_planner_backlog_stats(stats)


def _archived_skipped_count(session: Session, task: Task, business_filter) -> int:
    if task.type != "group_ai_chat":
        return 0
    count = session.scalar(
        select(func.count(Action.id)).where(
            Action.task_id == task.id,
            business_filter,
            Action.action_type == "send_message",
            Action.status == "skipped",
            Action.result["error_code"].as_string().in_(ARCHIVED_SKIP_ERROR_CODES),
        )
    )
    return int(count or 0)


def retry_failed_actions(session: Session, task: Task) -> int:
    policy = task.failure_policy or {}
    max_retries = int(policy.get("max_retries") or 0)
    if max_retries <= 0:
        return 0
    retry_delay = int(policy["retry_delay_seconds"]) if policy.get("retry_delay_seconds") is not None else 60
    backoff = policy.get("retry_backoff") or "none"
    count = 0
    query = select(Action).where(
        Action.task_id == task.id,
        Action.status.in_(["failed", "retryable_failed"]),
        Action.retry_count < max_retries,
    )
    for action in session.scalars(query):
        previous_result = dict(action.result or {})
        now_value = _now()
        if _skip_expired_hard_hourly_retry(action, previous_result, now_value):
            count += 1
            continue
        action.retry_count += 1
        delay = retry_delay
        if backoff == "linear":
            delay *= action.retry_count
        elif backoff == "exponential":
            delay *= 2 ** max(0, action.retry_count - 1)
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
        count += 1
    return count


def _skip_expired_hard_hourly_retry(action: Action, previous_result: dict[str, Any], now_value: datetime) -> bool:
    if not _hard_hourly_bucket_expired(action, now_value):
        return False
    action.status = "skipped"
    action.executed_at = now_value
    action.lease_owner = ""
    action.lease_expires_at = None
    action.result = {
        "success": False,
        "error_code": HARD_HOURLY_EXPIRED_ERROR_CODE,
        "error_message": HARD_HOURLY_EXPIRED_ERROR_MESSAGE,
        "validation_stage": "hard_hourly_retry_recovery",
        "auto_check": "过期补量跳过",
        "previous_result": previous_result,
    }
    return True


def _hard_hourly_bucket_expired(action: Action, now_value: datetime) -> bool:
    if action.action_type != "send_message":
        return False
    payload = action.payload if isinstance(action.payload, dict) else {}
    if not payload.get("hard_hourly_target"):
        return False
    bucket_value = str(payload.get("hard_hourly_bucket") or "").strip()
    if not bucket_value:
        return False
    try:
        bucket_start = datetime.fromisoformat(bucket_value)
    except ValueError:
        return False
    if bucket_start.tzinfo is None:
        comparable_now = now_value.replace(tzinfo=None)
    else:
        comparable_now = now_value.replace(tzinfo=BEIJING_TZ).astimezone(bucket_start.tzinfo)
    return bucket_start + timedelta(hours=1) <= comparable_now


def empty_stats() -> dict[str, Any]:
    return {
        "total_rounds": 0,
        "total_actions": 0,
        "success_count": 0,
        "failure_count": 0,
        "accounts_used": 0,
        "accounts_banned": 0,
        "started_at": None,
        "last_action_at": None,
        "estimated_completion": None,
    }


def utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _stats_datetime(task: Task, key: str) -> datetime | None:
    stats = task.stats or {}
    if not isinstance(stats, dict):
        return None
    value = stats.get(key)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return _naive_datetime(parsed)


def _naive_datetime(value):
    if value and getattr(value, "tzinfo", None):
        return value.replace(tzinfo=None)
    return value


__all__ = ["empty_stats", "next_run_after_task", "refresh_task_stats", "retry_failed_actions", "utc_now_naive"]
