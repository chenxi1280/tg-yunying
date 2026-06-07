from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, Task
from app.timezone import BEIJING_TZ

OPEN_STATUSES = {"pending", "claiming", "executing"}
SEND_FILTER = (Action.task_type == "group_ai_chat", Action.action_type == "send_message")
STRATEGY_FORCE_PLANNING = "force_planning"


def enabled(task_or_config: Task | dict[str, Any]) -> bool:
    config = task_or_config.type_config if isinstance(task_or_config, Task) else task_or_config
    return bool((config or {}).get("hard_hourly_target_enabled")) and goal(config or {}) > 0


def goal(config: dict[str, Any]) -> int:
    try:
        return max(0, int(config.get("hourly_min_messages") or 0))
    except (TypeError, ValueError):
        return 0


def current_progress(session: Session, task: Task, now: datetime) -> dict[str, Any]:
    stats = hard_hourly_stats(session, task, now, task.stats or {})
    now_local = normalize(task, now)
    return {
        "enabled": bool(stats.get("hard_hourly_target_enabled")),
        "goal": int(stats.get("hard_hourly_goal") or 0),
        "bucket": str(stats.get("hard_hourly_bucket") or ""),
        "deficit": int(stats.get("hard_hourly_deficit") or 0),
        "future_open_count": int(stats.get("hard_hourly_open_count") or 0),
        "overdue_open_count": int(stats.get("hard_hourly_overdue_open_count") or 0),
        "hour_end": hour_bounds(task, now)[1],
        "now": now_local,
    }


def requires_planning(session: Session, task: Task, now: datetime) -> bool:
    progress = current_progress(session, task, now)
    return bool(progress["enabled"]) and int(progress["deficit"]) > 0


def hard_hourly_stats(session: Session, task: Task, now: datetime, current_stats: dict[str, Any]) -> dict[str, Any]:
    if task.type != "group_ai_chat" or not enabled(task):
        return _disabled_stats(current_stats)
    now_local = normalize(task, now)
    bucket_start, bucket_end = hour_bounds(task, now)
    buckets = _recent_buckets(session, task, now_local, bucket_start)
    current = buckets[-1]
    last_blockers = dict(current.get("blockers") or current_stats.get("hard_hourly_last_blockers") or {})
    status = _current_status(current, now_local, bucket_end, last_blockers)
    updated = dict(current_stats)
    updated.update(_current_stat_values(task, now_local, current, status))
    updated["hard_hourly_last_blockers"] = last_blockers
    updated["hard_hourly_recent_buckets"] = buckets
    if int(current.get("deficit") or 0) <= 0:
        updated.pop("hard_hourly_next_check_at", None)
        updated.pop("hard_hourly_last_blockers", None)
    return updated


def hard_schedule_times(total: int, task: Task, now: datetime) -> list[datetime]:
    if total <= 0:
        return []
    current = normalize(task, now)
    _start, hour_end = hour_bounds(task, current)
    available = max(0, int((hour_end - current).total_seconds()) - 1)
    if available <= 0 or total == 1:
        return [current for _ in range(total)]
    step = max(1, available // max(total, 1))
    return [min(current + timedelta(seconds=step * index), hour_end - timedelta(seconds=1)) for index in range(total)]


def mark_plan_result(task: Task, progress: dict[str, Any], created: int, blockers: dict[str, int] | None = None) -> None:
    stats = dict(task.stats or {})
    current = progress.get("now")
    current = current if isinstance(current, datetime) else normalize(task, datetime.now())
    stats["hard_hourly_last_check_at"] = current.isoformat()
    stats["hard_hourly_last_planned_count"] = int(created)
    if blockers:
        stats["hard_hourly_last_blockers"] = blockers
    elif created > 0:
        stats.pop("hard_hourly_last_blockers", None)
    stats["hard_hourly_next_check_at"] = _next_check_at(task, blockers or {}, progress, current).isoformat()
    task.stats = stats


def normalize(task: Task, value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("datetime value is required")
    if value.tzinfo is None:
        return value
    return value.astimezone(_task_zone(task)).replace(tzinfo=None)


def hour_bounds(task: Task, value: datetime) -> tuple[datetime, datetime]:
    current = normalize(task, value)
    start = current.replace(minute=0, second=0, microsecond=0)
    return start, start + timedelta(hours=1)


def bucket_iso(task: Task, bucket_start: datetime) -> str:
    return bucket_start.replace(tzinfo=_task_zone(task)).isoformat()


def _disabled_stats(stats: dict[str, Any]) -> dict[str, Any]:
    updated = dict(stats)
    updated.update({"hard_hourly_target_enabled": False, "hard_hourly_status": "disabled"})
    return updated


def _recent_buckets(session: Session, task: Task, now_local: datetime, current_start: datetime) -> list[dict[str, Any]]:
    actions = _recent_actions(session, task, current_start - timedelta(hours=23))
    return [_bucket_summary(task, actions, current_start - timedelta(hours=offset), now_local) for offset in reversed(range(24))]


def _recent_actions(session: Session, task: Task, earliest: datetime) -> list[Action]:
    return list(
        session.scalars(
            select(Action).where(
                Action.task_id == task.id,
                *SEND_FILTER,
                (Action.executed_at >= earliest) | (Action.scheduled_at >= earliest),
            )
        )
    )


def _bucket_summary(task: Task, actions: list[Action], start: datetime, now_local: datetime) -> dict[str, Any]:
    end = start + timedelta(hours=1)
    success = sum(1 for action in actions if _is_success_in_bucket(task, action, start, end))
    future_open = sum(1 for action in actions if _is_future_open_in_bucket(task, action, start, end, now_local))
    overdue_open = sum(1 for action in actions if _is_overdue_open_in_bucket(task, action, start, end, now_local))
    deficit = max(0, goal(task.type_config or {}) - success - future_open)
    return {
        "bucket": bucket_iso(task, start),
        "goal": goal(task.type_config or {}),
        "success_count": success,
        "future_open_count": future_open,
        "overdue_open_count": overdue_open,
        "deficit": deficit,
        "status": _bucket_status(success, deficit, overdue_open, start, end, now_local),
        "blockers": {"dispatcher_lag": overdue_open} if overdue_open and deficit else {},
    }


def _is_success_in_bucket(task: Task, action: Action, start: datetime, end: datetime) -> bool:
    executed_at = _normalize_optional(task, action.executed_at)
    return action.status == "success" and executed_at is not None and start <= executed_at < end


def _is_future_open_in_bucket(task: Task, action: Action, start: datetime, end: datetime, now_local: datetime) -> bool:
    scheduled_at = _normalize_optional(task, action.scheduled_at)
    return action.status in OPEN_STATUSES and scheduled_at is not None and start <= scheduled_at < end and scheduled_at >= now_local


def _is_overdue_open_in_bucket(task: Task, action: Action, start: datetime, end: datetime, now_local: datetime) -> bool:
    scheduled_at = _normalize_optional(task, action.scheduled_at)
    return action.status in OPEN_STATUSES and scheduled_at is not None and start <= scheduled_at < end and scheduled_at < now_local


def _bucket_status(success: int, deficit: int, overdue: int, start: datetime, end: datetime, now_local: datetime) -> str:
    if deficit <= 0:
        return "met"
    if end <= now_local:
        return "missed"
    if overdue and start <= now_local < end:
        return "blocked"
    return "catching_up"


def _current_status(bucket: dict[str, Any], now_local: datetime, hour_end: datetime, blockers: dict[str, Any]) -> str:
    if int(bucket["success_count"]) >= int(bucket["goal"]):
        return "met"
    if hour_end <= now_local:
        return "missed"
    if int(bucket.get("overdue_open_count") or 0) and int(bucket.get("deficit") or 0):
        return "blocked"
    if blockers and not int(bucket["future_open_count"]):
        return "blocked"
    return "catching_up"


def _current_stat_values(task: Task, now_local: datetime, bucket: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "hard_hourly_target_enabled": True,
        "hard_hourly_goal": goal(task.type_config or {}),
        "hard_hourly_bucket": bucket["bucket"],
        "hard_hourly_success_count": bucket["success_count"],
        "hard_hourly_open_count": bucket["future_open_count"],
        "hard_hourly_overdue_open_count": bucket["overdue_open_count"],
        "hard_hourly_deficit": bucket["deficit"],
        "hard_hourly_status": status,
    }


def _next_check_at(task: Task, blockers: dict[str, int], progress: dict[str, Any], current: datetime) -> datetime:
    if blockers.get("ai_generation_unavailable"):
        return current + timedelta(minutes=1)
    if blockers.get("quality_filter"):
        return current + timedelta(seconds=60)
    if blockers.get("dispatcher_lag"):
        return current + timedelta(seconds=30)
    return current + timedelta(seconds=30 if int(progress.get("deficit") or 0) else 300)


def _task_zone(task: Task) -> ZoneInfo:
    try:
        return ZoneInfo(str(task.timezone or "Asia/Shanghai"))
    except ZoneInfoNotFoundError:
        return BEIJING_TZ


def _normalize_optional(task: Task, value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return normalize(task, value)
