from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, Task
from app.timezone import beijing_now


ACTION_TYPE_BY_TASK_TYPE = {
    "search_join_group": "search_join",
    "search_rank_deboost": "search_rank_deboost",
}
HELD_ACTION_STATUSES = (
    "pending",
    "claiming",
    "executing",
    "unknown_after_send",
)
TERMINAL_TASK_STATUSES = {"stopped", "failed", "deleted"}
SOURCE_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class SearchClickTargetProgress:
    target_count: int | None
    confirmed_count: int
    held_count: int
    remaining_slot_count: int | None
    scope: str = "lifecycle"
    local_date: str | None = None

    @property
    def is_daily_target(self) -> bool:
        return self.scope == "daily"

    @property
    def completed(self) -> bool:
        return (
            not self.is_daily_target
            and self.target_count is not None
            and self.confirmed_count >= self.target_count
        )

    @property
    def state(self) -> str:
        if self.completed:
            return "completed"
        if self.target_count is None:
            return "legacy_unlimited"
        if self.is_daily_target and self.confirmed_count >= self.target_count:
            return "daily_target_met"
        return "waiting_confirmation" if self.held_count else "planning"

    def as_dict(self) -> dict[str, int | str | None]:
        progress = {
            "target_count": self.target_count,
            "confirmed_count": self.confirmed_count,
            "held_count": self.held_count,
            "remaining_slot_count": self.remaining_slot_count,
            "state": self.state,
        }
        if self.is_daily_target:
            progress["scope"] = "daily"
            progress["local_date"] = self.local_date
        return progress


def search_click_target_progress(
    session: Session,
    task: Task,
    *,
    now_value: datetime | None = None,
) -> SearchClickTargetProgress:
    daily_target_count = _daily_target_count(task)
    action_type = _action_type(task)
    if daily_target_count is not None:
        start_at, end_at, local_date = _local_day_bounds(task, now_value or beijing_now())
        confirmed_count = _confirmed_action_count(
            session, task, action_type, start_at=start_at, end_at=end_at
        )
        held_count = _action_count(
            session, task, action_type, HELD_ACTION_STATUSES, start_at=start_at, end_at=end_at
        )
        remaining = _remaining_slots(daily_target_count, confirmed_count, held_count)
        return SearchClickTargetProgress(
            daily_target_count,
            confirmed_count,
            held_count,
            remaining,
            "daily",
            local_date,
        )
    target_count = _target_count(task)
    confirmed_count = _confirmed_action_count(session, task, action_type)
    held_count = _action_count(session, task, action_type, HELD_ACTION_STATUSES)
    remaining = _remaining_slots(target_count, confirmed_count, held_count)
    return SearchClickTargetProgress(target_count, confirmed_count, held_count, remaining)


def reconcile_search_click_target_progress(
    session: Session,
    task: Task,
    *,
    now_value: datetime | None = None,
) -> SearchClickTargetProgress:
    progress = search_click_target_progress(session, task, now_value=now_value)
    if progress.target_count is None:
        return progress
    stats = dict(task.stats or {})
    stats["search_click_target"] = progress.as_dict()
    if progress.is_daily_target:
        if stats.get("completion_reason") == "target_count_reached":
            stats.pop("completion_reason")
        task.stats = stats
        return progress
    if progress.completed:
        stats["completion_reason"] = "target_count_reached"
        if task.status not in TERMINAL_TASK_STATUSES:
            task.status = "completed"
            task.next_run_at = None
    elif stats.get("completion_reason") == "target_count_reached":
        stats.pop("completion_reason")
    task.stats = stats
    return progress


def _target_count(task: Task) -> int | None:
    value = (task.type_config or {}).get("target_count")
    if value is None:
        return None
    try:
        target_count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("search_click_target_count_invalid") from exc
    if target_count <= 0:
        raise ValueError("search_click_target_count_invalid")
    return target_count


def _daily_target_count(task: Task) -> int | None:
    if task.type != "search_join_group":
        return None
    value = (task.type_config or {}).get("daily_target_count")
    if value is None:
        return None
    try:
        target_count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("search_click_daily_target_count_invalid") from exc
    if target_count <= 0:
        raise ValueError("search_click_daily_target_count_invalid")
    return target_count


def _action_type(task: Task) -> str:
    action_type = ACTION_TYPE_BY_TASK_TYPE.get(task.type)
    if action_type is None:
        raise ValueError(f"search_click_target_type_unsupported:{task.type}")
    return action_type


def _action_count(
    session: Session,
    task: Task,
    action_type: str,
    statuses: tuple[str, ...],
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> int:
    filters = [
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        Action.action_type == action_type,
        Action.status.in_(statuses),
    ]
    _append_time_window(filters, start_at, end_at)
    count = session.scalar(
        select(func.count(Action.id)).where(*filters)
    )
    return int(count or 0)


def _confirmed_action_count(
    session: Session,
    task: Task,
    action_type: str,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> int:
    filters = [
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        Action.action_type == action_type,
        Action.status == "success",
    ]
    _append_time_window(filters, start_at, end_at)
    actions = session.scalars(select(Action).where(*filters))
    return sum(_has_confirmed_click_fact(task.type, action.result) for action in actions)


def _append_time_window(filters: list, start_at: datetime | None, end_at: datetime | None) -> None:
    if start_at is None or end_at is None:
        return
    action_at = func.coalesce(Action.executed_at, Action.scheduled_at)
    filters.extend((action_at >= start_at, action_at < end_at))


def _local_day_bounds(task: Task, now_value: datetime) -> tuple[datetime, datetime, str]:
    source_now = now_value if now_value.tzinfo else now_value.replace(tzinfo=SOURCE_TIMEZONE)
    timezone = ZoneInfo(task.timezone or "Asia/Shanghai")
    local_now = source_now.astimezone(timezone)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        _source_naive(local_start),
        _source_naive(local_start + timedelta(days=1)),
        local_now.date().isoformat(),
    )


def _source_naive(value: datetime) -> datetime:
    return value.astimezone(SOURCE_TIMEZONE).replace(tzinfo=None)


def _has_confirmed_click_fact(task_type: str, result: object) -> bool:
    if not isinstance(result, dict):
        return False
    if task_type == "search_join_group":
        return result.get("join_status") == "membership_observed" or result.get("membership_observed") is True
    if task_type == "search_rank_deboost":
        return _has_confirmed_rank_deboost_click_fact(result)
    return False


def _has_confirmed_rank_deboost_click_fact(result: dict) -> bool:
    outcomes = result.get("click_outcomes")
    if result.get("execution_status") != "confirmed" or not isinstance(outcomes, list) or len(outcomes) != 1:
        return False
    outcome = outcomes[0]
    if not isinstance(outcome, dict) or outcome.get("status") != "confirmed":
        return False
    identity = str(outcome.get("competitor_username") or outcome.get("competitor_peer_id") or "").strip()
    required = ("competitor_position", "row", "col", "dwell_seconds", "effect", "joined")
    try:
        position = int(outcome.get("competitor_position") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        identity
        and position > 0
        and all(key in outcome for key in required)
        and outcome.get("effect") == "navigate_only"
        and outcome.get("joined") is False
    )


def _remaining_slots(target_count: int | None, confirmed_count: int, held_count: int) -> int | None:
    if target_count is None:
        return None
    return max(0, target_count - confirmed_count - held_count)


__all__ = [
    "SearchClickTargetProgress",
    "reconcile_search_click_target_progress",
    "search_click_target_progress",
]
