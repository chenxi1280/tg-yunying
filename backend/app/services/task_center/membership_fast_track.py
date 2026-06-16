from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, Task
from app.services._common import _now

MEMBERSHIP_ACTION_TYPES = ("ensure_target_membership", "ensure_channel_membership")
FAST_TRACK_INTERVAL_SECONDS = 2


def fast_track_pending_hard_hourly_memberships(session: Session, *, limit: int) -> int:
    now_value = _now()
    rows = list(
        session.execute(
            select(Action)
            .add_columns(Task.type_config)
            .join(Task, Task.id == Action.task_id)
            .where(
                Task.type == "group_ai_chat",
                Action.action_type.in_(MEMBERSHIP_ACTION_TYPES),
                Action.status == "pending",
                Action.scheduled_at > now_value,
            )
            .order_by(Action.scheduled_at.asc(), Action.created_at.asc())
            .limit(max(1, int(limit or 1)))
        )
    )
    filtered = [action for action, config in rows if _hard_hourly_enabled(config or {})]
    task_counts: dict[str, int] = {}
    for index, action in enumerate(filtered):
        action.scheduled_at = now_value + timedelta(seconds=FAST_TRACK_INTERVAL_SECONDS * index)
        action.result = {**(action.result or {}), "fast_tracked_reason": "recovery_hard_hourly_membership"}
        task_counts[str(action.task_id or "")] = task_counts.get(str(action.task_id or ""), 0) + 1
    _record_task_counts(session, task_counts)
    return len(filtered)


def _hard_hourly_enabled(config: dict) -> bool:
    try:
        return bool(config.get("hard_hourly_target_enabled")) and int(config.get("hourly_min_messages") or 0) > 0
    except (TypeError, ValueError):
        return False


def _record_task_counts(session: Session, task_counts: dict[str, int]) -> None:
    for task_id, count in task_counts.items():
        if not task_id:
            continue
        task = session.get(Task, task_id)
        if not task:
            continue
        stats = dict(task.stats or {})
        stats["membership_recovery_fast_tracked_actions"] = int(stats.get("membership_recovery_fast_tracked_actions") or 0) + count
        task.stats = stats
