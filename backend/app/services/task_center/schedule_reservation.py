from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, Task

from .pacing import minimum_schedule_gap_seconds


OPEN_PACING_RESERVATION_STATUSES = (
    "pending",
    "claiming",
    "executing",
    "retryable_failed",
    "unknown_after_send",
)


def reserve_task_schedule_times(
    session: Session,
    task: Task,
    action_type: str,
    planned_times: list[datetime],
    *,
    pacing_config: dict,
    deadline_at: datetime | None = None,
    enforce_task_spacing: bool = True,
) -> list[datetime]:
    if not planned_times:
        return []
    if not enforce_task_spacing:
        return _before_deadline(planned_times, deadline_at)
    latest_open = session.scalar(
        select(func.max(Action.scheduled_at)).where(
            Action.task_id == task.id,
            Action.action_type == action_type,
            Action.status.in_(OPEN_PACING_RESERVATION_STATUSES),
        )
    )
    latest_success = session.scalar(
        select(func.max(Action.executed_at)).where(
            Action.task_id == task.id,
            Action.action_type == action_type,
            Action.status == "success",
            Action.executed_at.is_not(None),
        )
    )
    return continue_schedule_after(
        planned_times,
        latest_scheduled_at=_latest_anchor(latest_open, latest_success),
        minimum_gap_seconds=minimum_schedule_gap_seconds(pacing_config),
        deadline_at=deadline_at,
    )


def continue_schedule_after(
    planned_times: list[datetime],
    *,
    latest_scheduled_at: datetime | None,
    minimum_gap_seconds: int,
    deadline_at: datetime | None = None,
) -> list[datetime]:
    if not planned_times:
        return []
    ordered = sorted(planned_times)
    latest = _match_timezone(latest_scheduled_at, ordered[0])
    if latest is not None:
        floor = latest + timedelta(seconds=max(1, minimum_gap_seconds))
        if ordered[0] < floor:
            shift = floor - ordered[0]
            ordered = [item + shift for item in ordered]
    return _before_deadline(ordered, deadline_at)


def _before_deadline(
    planned_times: list[datetime],
    deadline_at: datetime | None,
) -> list[datetime]:
    ordered = sorted(planned_times)
    if not ordered or deadline_at is None:
        return ordered
    deadline = _match_timezone(deadline_at, ordered[0])
    return [item for item in ordered if deadline is not None and item < deadline]


def _match_timezone(value: datetime | None, reference: datetime) -> datetime | None:
    if value is None or value.tzinfo is reference.tzinfo:
        return value
    if reference.tzinfo is None:
        return value.replace(tzinfo=None)
    if value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo)
    return value.astimezone(reference.tzinfo)


def _latest_anchor(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


__all__ = ["continue_schedule_after", "reserve_task_schedule_times"]
