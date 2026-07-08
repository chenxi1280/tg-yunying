from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Action, Task
from app.services._common import _now
from app.timezone import BEIJING_TZ, as_beijing

from .config_fields import CHANNEL_DYNAMIC_TASK_TYPES
from .hard_hourly import enabled as hard_hourly_enabled, hard_hourly_stats
from .pacing import ai_next_run_after, next_run_after
from .search_join_config import runtime_search_join_config

ARCHIVED_SKIP_ERROR_CODES = {"context_expired"}
DEFAULT_AUTO_RETRY_STATUSES = ("failed", "retryable_failed")
TARGET_ADMISSION_AUTO_RETRY_STATUSES = ("failed", "retryable_failed")
TARGET_ADMISSION_DEFAULT_MAX_RETRIES = 1
TARGET_ADMISSION_DEFAULT_RETRY_DELAY_SECONDS = 30
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
SEARCH_JOIN_OPEN_STATUSES = ("pending", "claiming", "executing")
AI_GROUP_TERMINAL_QUALITY_ERRORS = frozenset({"duplicate_message", "ai_message_memory_missing"})
AI_GROUP_TERMINAL_GENERATION_STATUSES = frozenset({"duplicate_rejected"})


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
    business_filter = _stats_action_filter(task)
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
    stats = _search_join_stats(session, task, stats)
    task.stats = stats
    from app.services.runtime_summary import refresh_task_summary

    refresh_task_summary(session, task)
    return stats


def _search_join_stats(session: Session, task: Task, stats: dict[str, Any]) -> dict[str, Any]:
    if task.type != "search_join_group":
        return stats
    updated = dict(stats)
    search_join_stats = dict(updated.get("search_join_stats") or {})
    previous_hourly = dict(search_join_stats.get("hourly_execution") or {})
    current_hourly = search_join_hourly_execution(session, task, _now())
    search_join_stats["hourly_execution"] = {**previous_hourly, **current_hourly}
    updated["search_join_stats"] = search_join_stats
    return updated


def search_join_hourly_execution(session: Session, task: Task, now_value: datetime) -> dict[str, Any]:
    config = runtime_search_join_config(task)
    bucket_start = now_value.replace(minute=0, second=0, microsecond=0)
    bucket_end = bucket_start + timedelta(hours=1)
    success_count = _search_join_success_count(session, task, bucket_start, bucket_end)
    future_open = _search_join_open_count(session, task, now_value, bucket_end, overdue=False)
    overdue_open = _search_join_open_count(session, task, now_value, bucket_end, overdue=True)
    goal = int(config.get("hourly_min_successful_joins") or 0)
    max_actions = int(config.get("max_actions_per_hour") or 0)
    deficit = max(0, goal - success_count - future_open)
    capacity = max(0, max_actions - success_count - future_open - overdue_open)
    return {
        "bucket": bucket_start.isoformat(),
        "status": _search_join_hourly_status(goal, deficit, capacity),
        "goal": goal,
        "success_count": success_count,
        "future_open_count": future_open,
        "overdue_open_count": overdue_open,
        "deficit": deficit,
        "capacity": capacity,
        "max_actions_per_hour": max_actions,
    }


def _search_join_success_count(session: Session, task: Task, start: datetime, end: datetime) -> int:
    return int(
        session.scalar(
            select(func.count(Action.id)).where(
                Action.task_id == task.id,
                Action.action_type == "search_join",
                Action.status == "success",
                Action.executed_at >= start,
                Action.executed_at < end,
            )
        )
        or 0
    )


def _search_join_open_count(session: Session, task: Task, now_value: datetime, bucket_end: datetime, *, overdue: bool) -> int:
    boundary = Action.scheduled_at < now_value if overdue else Action.scheduled_at >= now_value
    upper = true() if overdue else Action.scheduled_at < bucket_end
    return int(
        session.scalar(
            select(func.count(Action.id)).where(
                Action.task_id == task.id,
                Action.action_type == "search_join",
                Action.status.in_(SEARCH_JOIN_OPEN_STATUSES),
                boundary,
                upper,
            )
        )
        or 0
    )


def _search_join_hourly_status(goal: int, deficit: int, capacity: int) -> str:
    if goal <= 0:
        return "open"
    if deficit <= 0:
        return "met"
    if capacity <= 0:
        return "blocked"
    return "catching_up"


def _stats_action_filter(task: Task):
    if task.type == "target_admission_retry":
        return true()
    return Action.action_type.notin_(BUSINESS_MEMBERSHIP_ACTION_TYPES)


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
    max_retries = _max_retries_for_task(task, policy)
    if max_retries <= 0:
        return 0
    retry_delay = _retry_delay_seconds_for_task(task, policy)
    backoff = policy.get("retry_backoff") or "none"
    count = 0
    query = select(Action).where(
        Action.task_id == task.id,
        Action.status.in_(_auto_retry_statuses(task)),
        Action.retry_count < max_retries,
    )
    for action in session.scalars(query):
        previous_result = dict(action.result or {})
        if _is_terminal_ai_quality_failure(action, previous_result):
            continue
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


def _is_terminal_ai_quality_failure(action: Action, previous_result: dict[str, Any]) -> bool:
    if action.task_type != "group_ai_chat" or action.action_type != "send_message":
        return False
    payload = action.payload if isinstance(action.payload, dict) else {}
    error_code = str(previous_result.get("error_code") or previous_result.get("failure_type") or "")
    generation_status = str(payload.get("ai_generation_status") or "")
    quality_reason = str(payload.get("quality_skip_reason") or previous_result.get("quality_skip_reason") or "")
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
