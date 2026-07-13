from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Action, Task
from app.services._common import _now
from app.timezone import BEIJING_TZ, as_beijing

from .hard_hourly import enabled as hard_hourly_enabled

BACKLOG_MEMBERSHIP_ACTION_TYPES = ["ensure_channel_membership", "ensure_target_membership"]
PLANNER_BACKLOG_OPEN_STATUSES = {"pending", "claiming", "executing"}


def planner_backlog_snapshot(session: Session, task: Task) -> dict[str, int | bool]:
    settings = get_settings()
    task_filters = [Action.task_id == task.id, Action.status.in_(PLANNER_BACKLOG_OPEN_STATUSES)]
    if _can_plan_with_partial_membership(task):
        task_filters.append(Action.action_type.notin_(BACKLOG_MEMBERSHIP_ACTION_TYPES))
    now_value = _now()
    global_pending, _ = _active_backlog_metrics(
        session,
        [Action.status.in_(PLANNER_BACKLOG_OPEN_STATUSES)],
        now_value,
    )
    task_pending, oldest_pending = _active_backlog_metrics(session, task_filters, now_value)
    oldest_at = as_beijing(oldest_pending)
    oldest_age = int((_now() - oldest_at).total_seconds()) if oldest_at else 0
    blocked = (
        global_pending >= int(settings.max_pending_global or 0)
        or task_pending >= int(settings.max_pending_per_task or 0)
        or oldest_age >= int(settings.oldest_pending_age_seconds or 0)
    )
    return {
        "blocked": blocked,
        "global_pending": global_pending,
        "task_pending": task_pending,
        "oldest_age_seconds": oldest_age,
    }


def _active_backlog_metrics(session: Session, filters: list[Any], now_value: datetime) -> tuple[int, datetime | None]:
    hard_target = Action.payload["hard_hourly_target"].as_boolean().is_(True)
    hard_filter = (Action.action_type == "send_message") & hard_target
    normal_count, normal_oldest = session.execute(
        select(func.count(Action.id), func.min(Action.scheduled_at)).where(*filters, ~hard_filter)
    ).one()
    hard_rows = session.execute(
        select(Action.scheduled_at, Action.payload).where(*filters, hard_filter)
    ).all()
    active_hard_times = [
        scheduled_at
        for scheduled_at, payload in hard_rows
        if not hard_hourly_payload_expired(payload, now_value)
    ]
    oldest_candidates = [value for value in [normal_oldest, *active_hard_times] if value is not None]
    return int(normal_count or 0) + len(active_hard_times), min(oldest_candidates, default=None)


def hard_hourly_payload_expired(payload: dict[str, Any], now_value: datetime) -> bool:
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


__all__ = ["hard_hourly_payload_expired", "planner_backlog_snapshot"]
