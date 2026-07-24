from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, Task
from app.services._common import _now

from .channel_membership import DAILY_PERMISSION_RECHECK_REASONS, is_daily_permission_recheck_action
from .dispatcher import claim_action_ordering

MEMBERSHIP_ACTION_TYPES = ("ensure_target_membership", "ensure_channel_membership")
FAST_TRACK_INTERVAL_SECONDS = 2
FAST_TRACK_BATCH_SIZE = 50


@dataclass(frozen=True)
class FastTrackResult:
    processed: int
    task_counts: dict[str, int]


def fast_track_pending_hard_hourly_memberships(session: Session, *, limit: int) -> FastTrackResult:
    """Pull future hard-hourly membership actions forward in claim-aligned order, batched."""
    now_value = _now()
    batch_limit = max(1, min(int(limit or 1), FAST_TRACK_BATCH_SIZE))
    statement = (
        select(Action)
        .add_columns(Task.type_config)
        .join(Task, Task.id == Action.task_id)
        .where(
            Task.type == "group_ai_chat",
            Action.action_type.in_(MEMBERSHIP_ACTION_TYPES),
            Action.status == "pending",
            Action.scheduled_at > now_value,
            func.coalesce(Action.result["reactivated_reason"].as_string(), "").notin_(DAILY_PERMISSION_RECHECK_REASONS),
        )
        # Use the same complete Action lock order as Dispatcher claim.  Keeping
        # the rank expression here matters even though this query only selects
        # hard-hourly membership rows: it prevents this path drifting from the
        # claim lock sequence as ranks evolve.
        .order_by(*claim_action_ordering(set(), now_value))
        .limit(batch_limit)
    )
    if session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update(of=Action, skip_locked=True)
    rows = list(session.execute(statement))
    filtered = [
        action for action, config in rows
        if _hard_hourly_enabled(config or {}) and not is_daily_permission_recheck_action(action)
    ]
    task_counts: dict[str, int] = {}
    processed = 0
    for index, action in enumerate(filtered):
        action.scheduled_at = now_value + timedelta(seconds=FAST_TRACK_INTERVAL_SECONDS * index)
        action.result = {**(action.result or {}), "fast_tracked_reason": "recovery_hard_hourly_membership"}
        task_counts[str(action.task_id or "")] = task_counts.get(str(action.task_id or ""), 0) + 1
        processed += 1
    return FastTrackResult(processed=processed, task_counts=task_counts)


def _hard_hourly_enabled(config: dict) -> bool:
    try:
        return bool(config.get("hard_hourly_target_enabled")) and int(config.get("hourly_min_messages") or 0) > 0
    except (TypeError, ValueError):
        return False


def record_fast_track_task_counts(session: Session, task_counts: dict[str, int]) -> None:
    """Persist fast-track counters only after the Action-lock transaction commits."""
    for task_id, count in task_counts.items():
        if not task_id:
            continue
        task = session.get(Task, task_id)
        if not task:
            continue
        stats = dict(task.stats or {})
        stats["membership_recovery_fast_tracked_actions"] = int(stats.get("membership_recovery_fast_tracked_actions") or 0) + count
        task.stats = stats
