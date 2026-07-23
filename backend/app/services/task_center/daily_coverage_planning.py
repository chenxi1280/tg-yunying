from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, or_, select, tuple_
from sqlalchemy.orm import Session

from app.models import Task, TaskAccountDailyCoverage, TaskDailyCoveragePlanCursor
from app.services._common import _now

from .daily_coverage_schedule import daily_coverage_due_debt_totals


MAX_DAILY_COVERAGE_PLAN_BATCH = 20
SENDABLE_COVERAGE_STATES = ("ready", "reserved", "sending", "unknown")


@dataclass(frozen=True)
class CoveragePlanBatch:
    rows: list[TaskAccountDailyCoverage]
    wrapped: bool


@dataclass(frozen=True)
class CoveragePlanTotals:
    account_count: int
    required_count: int
    confirmed_count: int
    reserved_count: int
    sendable_account_count: int
    sendable_confirmed_count: int
    sendable_reserved_count: int
    target_per_account: int
    due_debt: int


def ready_coverage_plan_batch(
    session: Session,
    task: Task,
    *,
    now: datetime | None = None,
    limit: int = MAX_DAILY_COVERAGE_PLAN_BATCH,
) -> CoveragePlanBatch:
    timestamp = now or _now()
    batch_limit = min(MAX_DAILY_COVERAGE_PLAN_BATCH, max(1, int(limit)))
    cursor = _locked_cursor(session, task, timestamp)
    rows = _ready_rows_after_cursor(session, task, cursor, timestamp, batch_limit)
    if rows or not cursor.last_coverage_id:
        return CoveragePlanBatch(rows=rows, wrapped=False)
    _rewind_cursor(cursor, timestamp)
    rows = _ready_rows_after_cursor(session, task, cursor, timestamp, batch_limit)
    return CoveragePlanBatch(rows=rows, wrapped=True)


def advance_coverage_plan_cursor(
    session: Session,
    task: Task,
    row: TaskAccountDailyCoverage,
    *,
    now: datetime | None = None,
) -> None:
    timestamp = now or _now()
    if row.task_id != task.id or row.coverage_date != timestamp.date():
        raise ValueError("coverage cursor row does not belong to task day")
    cursor = _locked_cursor(session, task, timestamp)
    cursor.last_targeted_at = row.targeted_at
    cursor.last_account_id = row.account_id
    cursor.last_coverage_id = row.id
    cursor.version = int(cursor.version or 0) + 1
    cursor.updated_at = timestamp


def coverage_plan_totals(
    session: Session,
    task: Task,
    group,
    *,
    now: datetime | None = None,
) -> CoveragePlanTotals:
    timestamp = now or _now()
    values = _coverage_totals_row(session, task, timestamp)
    required, confirmed, reserved = values[1:4]
    due_debt = daily_coverage_due_debt_totals(
        task,
        group,
        timestamp.date(),
        required=required,
        confirmed=confirmed,
        reserved=reserved,
        now=timestamp,
    )
    return CoveragePlanTotals(*values, due_debt=due_debt)


def _coverage_totals_row(session: Session, task: Task, timestamp: datetime) -> tuple[int, ...]:
    sendable_state = TaskAccountDailyCoverage.state.in_(SENDABLE_COVERAGE_STATES)
    reserved_case = case(
        (TaskAccountDailyCoverage.state.in_(("reserved", "sending")), 1),
        else_=0,
    )
    sendable_reserved_case = case(
        (TaskAccountDailyCoverage.state.in_(("reserved", "sending", "unknown")), 1),
        else_=0,
    )
    row = session.execute(
        select(
            func.count(TaskAccountDailyCoverage.id),
            func.coalesce(func.sum(TaskAccountDailyCoverage.target_count), 0),
            func.coalesce(func.sum(TaskAccountDailyCoverage.confirmed_count), 0),
            func.coalesce(func.sum(reserved_case), 0),
            func.coalesce(func.sum(case((sendable_state, 1), else_=0)), 0),
            func.coalesce(
                func.sum(case((sendable_state, TaskAccountDailyCoverage.confirmed_count), else_=0)),
                0,
            ),
            func.coalesce(func.sum(sendable_reserved_case), 0),
            func.coalesce(func.max(TaskAccountDailyCoverage.target_count), 1),
        ).where(
            TaskAccountDailyCoverage.tenant_id == task.tenant_id,
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.coverage_date == timestamp.date(),
        )
    ).one()
    return tuple(map(int, row))


def _locked_cursor(
    session: Session,
    task: Task,
    timestamp: datetime,
) -> TaskDailyCoveragePlanCursor:
    _lock_task(session, task.id)
    cursor = session.scalar(
        select(TaskDailyCoveragePlanCursor)
        .where(
            TaskDailyCoveragePlanCursor.tenant_id == task.tenant_id,
            TaskDailyCoveragePlanCursor.task_id == task.id,
            TaskDailyCoveragePlanCursor.coverage_date == timestamp.date(),
        )
        .with_for_update()
    )
    if cursor is not None:
        return cursor
    cursor = TaskDailyCoveragePlanCursor(
        tenant_id=task.tenant_id,
        task_id=task.id,
        coverage_date=timestamp.date(),
    )
    session.add(cursor)
    session.flush()
    return cursor


def _lock_task(session: Session, task_id: str) -> None:
    session.execute(select(Task.id).where(Task.id == task_id).with_for_update()).scalar_one()


def _ready_rows_after_cursor(
    session: Session,
    task: Task,
    cursor: TaskDailyCoveragePlanCursor,
    timestamp: datetime,
    limit: int,
) -> list[TaskAccountDailyCoverage]:
    filters = [
        TaskAccountDailyCoverage.tenant_id == task.tenant_id,
        TaskAccountDailyCoverage.task_id == task.id,
        TaskAccountDailyCoverage.coverage_date == timestamp.date(),
        TaskAccountDailyCoverage.state == "ready",
        TaskAccountDailyCoverage.confirmed_count < TaskAccountDailyCoverage.target_count,
        TaskAccountDailyCoverage.targeted_at <= timestamp,
        or_(
            TaskAccountDailyCoverage.next_eligible_at.is_(None),
            TaskAccountDailyCoverage.next_eligible_at <= timestamp,
        ),
    ]
    if cursor.last_targeted_at is not None and cursor.last_account_id is not None:
        filters.append(
            tuple_(
                TaskAccountDailyCoverage.targeted_at,
                TaskAccountDailyCoverage.account_id,
                TaskAccountDailyCoverage.id,
            )
            > tuple_(
                cursor.last_targeted_at,
                cursor.last_account_id,
                cursor.last_coverage_id,
            )
        )
    statement = (
        select(TaskAccountDailyCoverage)
        .where(*filters)
        .order_by(
            TaskAccountDailyCoverage.targeted_at.asc(),
            TaskAccountDailyCoverage.account_id.asc(),
            TaskAccountDailyCoverage.id.asc(),
        )
        .limit(limit)
    )
    if session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update(skip_locked=True)
    return list(session.scalars(statement))


def _rewind_cursor(cursor: TaskDailyCoveragePlanCursor, timestamp: datetime) -> None:
    cursor.last_targeted_at = None
    cursor.last_account_id = None
    cursor.last_coverage_id = ""
    cursor.wrap_count = int(cursor.wrap_count or 0) + 1
    cursor.version = int(cursor.version or 0) + 1
    cursor.updated_at = timestamp


__all__ = [
    "CoveragePlanBatch",
    "CoveragePlanTotals",
    "MAX_DAILY_COVERAGE_PLAN_BATCH",
    "SENDABLE_COVERAGE_STATES",
    "advance_coverage_plan_cursor",
    "coverage_plan_totals",
    "ready_coverage_plan_batch",
]
