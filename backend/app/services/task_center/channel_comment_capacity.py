from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CommentFulfillmentObligation,
    Task,
    TaskCommentCapacityPeriod,
    TaskCommentCapacityReservation,
)
from app.timezone import BEIJING_TZ


OPEN_CAPACITY_STATES = frozenset({
    "plan_reserved", "action_reserved", "gateway_hold", "confirmed",
})


def remaining_comment_capacity(
    session: Session,
    task: Task,
    daily_cap: int,
    *,
    at: datetime,
) -> int:
    period = _capacity_period(session, task, daily_cap=daily_cap, at=at)
    used = _used_capacity(session, period)
    return max(0, int(period.capacity_limit) - used)


def reserve_comment_capacity(
    session: Session,
    task: Task,
    obligation: CommentFulfillmentObligation,
    *,
    scheduled_at: datetime,
    daily_cap: int,
) -> TaskCommentCapacityReservation | None:
    if not obligation.plan_contract_id:
        raise RuntimeError("comment_capacity_plan_contract_missing")
    existing = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.obligation_id == obligation.id,
    ))
    if existing is not None and existing.reservation_state in OPEN_CAPACITY_STATES:
        return existing
    period = _capacity_period(
        session, task, daily_cap=daily_cap, at=scheduled_at,
    )
    if remaining_comment_capacity(
        session, task, daily_cap, at=scheduled_at,
    ) <= 0:
        return None
    reservation = existing or TaskCommentCapacityReservation(
        tenant_id=task.tenant_id,
        task_id=task.id,
        plan_contract_id=str(obligation.plan_contract_id),
        obligation_id=obligation.id,
    )
    reservation.capacity_period_id = period.id
    reservation.capacity_units = 1
    reservation.reservation_state = "plan_reserved"
    reservation.scheduled_for_at = scheduled_at
    session.add(reservation)
    session.flush()
    return reservation


def release_comment_capacity(
    session: Session,
    obligation_id: str,
) -> None:
    row = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.obligation_id == obligation_id,
    ))
    if row is not None and row.reservation_state in {"plan_reserved", "action_reserved"}:
        row.reservation_state = "released"


def bind_comment_capacity_action(
    session: Session,
    obligation_id: str,
    action_id: str,
) -> None:
    row = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.obligation_id == obligation_id,
    ))
    if row is None or row.reservation_state != "plan_reserved":
        raise RuntimeError("comment_capacity_reservation_missing")
    row.action_id = action_id
    row.reservation_state = "action_reserved"


def mark_comment_capacity_gateway_hold(session: Session, action_id: str) -> None:
    row = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.action_id == action_id,
    ))
    if row is not None and row.reservation_state == "action_reserved":
        row.reservation_state = "gateway_hold"


def settle_comment_capacity(
    session: Session,
    obligation_id: str,
    *,
    confirmed: bool,
) -> None:
    row = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.obligation_id == obligation_id,
    ))
    if row is None:
        return
    if confirmed:
        row.reservation_state = "confirmed"
    elif row.reservation_state in {"plan_reserved", "action_reserved"}:
        row.reservation_state = "released"


def _capacity_period(
    session: Session,
    task: Task,
    *,
    daily_cap: int,
    at: datetime,
) -> TaskCommentCapacityPeriod:
    period = _containing_period(session, task.id, at)
    if period is not None:
        return period
    latest = session.scalar(
        select(TaskCommentCapacityPeriod)
        .where(TaskCommentCapacityPeriod.task_id == task.id)
        .order_by(TaskCommentCapacityPeriod.period_end_at.desc())
        .limit(1)
    )
    period = latest or _initial_period(task, daily_cap=daily_cap, at=at)
    if latest is None:
        session.add(period)
        session.flush()
    while at.replace(tzinfo=None) >= period.period_end_at.replace(tzinfo=None):
        period = _next_period(task, period, daily_cap=daily_cap)
        session.add(period)
        session.flush()
    return period


def _containing_period(
    session: Session,
    task_id: str,
    at: datetime,
) -> TaskCommentCapacityPeriod | None:
    statement = select(TaskCommentCapacityPeriod).where(
        TaskCommentCapacityPeriod.task_id == task_id,
        TaskCommentCapacityPeriod.period_start_at <= at,
        TaskCommentCapacityPeriod.period_end_at > at,
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    return session.scalar(statement)


def _initial_period(task: Task, *, daily_cap: int, at: datetime) -> TaskCommentCapacityPeriod:
    timezone_name = task.timezone or "Asia/Shanghai"
    local_at = _task_local(at, timezone_name)
    local_start = local_at.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return _period_row(
        task,
        revision=1,
        timezone_name=timezone_name,
        start_at=_beijing_wall(local_start),
        end_at=_beijing_wall(local_end),
        capacity_limit=daily_cap,
    )


def _next_period(
    task: Task,
    previous: TaskCommentCapacityPeriod,
    *,
    daily_cap: int,
) -> TaskCommentCapacityPeriod:
    timezone_name = task.timezone or "Asia/Shanghai"
    start_at = previous.period_end_at
    local_start = _task_local(start_at, timezone_name)
    next_date = local_start.date() + timedelta(days=1)
    local_end = datetime.combine(next_date, datetime.min.time(), tzinfo=ZoneInfo(timezone_name))
    end_at = _beijing_wall(local_end)
    revision = int(previous.calendar_revision) + int(previous.timezone_snapshot != timezone_name)
    return _period_row(
        task,
        revision=revision,
        timezone_name=timezone_name,
        start_at=start_at,
        end_at=end_at,
        capacity_limit=_transition_capacity(local_start, daily_cap),
    )


def _period_row(
    task: Task,
    *,
    revision: int,
    timezone_name: str,
    start_at: datetime,
    end_at: datetime,
    capacity_limit: int,
) -> TaskCommentCapacityPeriod:
    return TaskCommentCapacityPeriod(
        tenant_id=task.tenant_id,
        task_id=task.id,
        calendar_revision=revision,
        timezone_snapshot=timezone_name,
        period_start_at=start_at,
        period_end_at=end_at,
        capacity_limit=max(0, int(capacity_limit)),
        period_state="open",
    )


def _transition_capacity(local_start: datetime, daily_cap: int) -> int:
    if local_start.time() == datetime.min.time():
        return daily_cap
    next_midnight = datetime.combine(
        local_start.date() + timedelta(days=1),
        datetime.min.time(),
        tzinfo=local_start.tzinfo,
    )
    seconds = (next_midnight - local_start).total_seconds()
    return math.floor(max(0, daily_cap) * seconds / timedelta(days=1).total_seconds())


def _used_capacity(session: Session, period: TaskCommentCapacityPeriod) -> int:
    return int(session.scalar(select(func.sum(
        TaskCommentCapacityReservation.capacity_units,
    )).where(
        TaskCommentCapacityReservation.capacity_period_id == period.id,
        TaskCommentCapacityReservation.reservation_state.in_(OPEN_CAPACITY_STATES),
    )) or 0)


def _task_local(value: datetime, timezone_name: str) -> datetime:
    return _beijing_aware(value).astimezone(ZoneInfo(timezone_name))


def _beijing_aware(value: datetime) -> datetime:
    return value.astimezone(BEIJING_TZ) if value.tzinfo else value.replace(tzinfo=BEIJING_TZ)


def _beijing_wall(value: datetime) -> datetime:
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)


__all__ = [
    "bind_comment_capacity_action",
    "mark_comment_capacity_gateway_hold",
    "release_comment_capacity",
    "remaining_comment_capacity",
    "reserve_comment_capacity",
    "settle_comment_capacity",
]
