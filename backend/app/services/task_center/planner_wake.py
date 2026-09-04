from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import Task, TaskPlannerWakeState
from app.services._common import _now
from app.timezone import as_beijing


def wake_task_planner(
    session: Session,
    task: Task,
    *,
    reason_code: str,
    not_before_at: datetime | None = None,
) -> TaskPlannerWakeState:
    state = _lock_or_create_wake_state(session, task)
    if state.lifecycle_epoch != int(task.task_lifecycle_epoch or 1):
        state.lifecycle_epoch = int(task.task_lifecycle_epoch or 1)
        state.planned_revision = 0
        state.planning_revision = 0
    state.wake_revision = int(state.wake_revision or 0) + 1
    state.not_before_at = _earliest_not_before(
        state.not_before_at,
        not_before_at or _now(),
    )
    state.reason_code = reason_code[:80]
    state.version = int(state.version or 1) + 1
    return state


def complete_task_planner_wake(
    session: Session,
    task: Task,
    *,
    next_run_at: datetime | None,
) -> None:
    state = _locked_wake_state(session, task)
    if state is None:
        state = wake_task_planner(
            session,
            task,
            reason_code="planner_bootstrap",
            not_before_at=next_run_at,
        )
    planning_revision = int(state.planning_revision or state.wake_revision or 0)
    late_wake = int(state.wake_revision or 0) > planning_revision
    state.planned_revision = planning_revision
    state.last_completed_at = _now()
    if not late_wake:
        state.not_before_at = next_run_at
    state.planning_revision = 0
    state.version = int(state.version or 1) + 1


def mark_task_planner_started(session: Session, task: Task) -> None:
    state = _locked_wake_state(session, task)
    if state is None:
        return
    state.planning_revision = int(state.wake_revision or 0)
    state.last_started_at = _now()
    state.version = int(state.version or 1) + 1


def _locked_wake_state(
    session: Session,
    task: Task,
) -> TaskPlannerWakeState | None:
    return session.scalar(
        select(TaskPlannerWakeState)
        .where(
            TaskPlannerWakeState.tenant_id == task.tenant_id,
            TaskPlannerWakeState.task_id == task.id,
        )
        .with_for_update()
    )


def _lock_or_create_wake_state(
    session: Session,
    task: Task,
) -> TaskPlannerWakeState:
    state = _locked_wake_state(session, task)
    if state is not None:
        return state
    values = {
        "id": str(uuid4()),
        "tenant_id": task.tenant_id,
        "task_id": task.id,
        "lifecycle_epoch": int(task.task_lifecycle_epoch or 1),
    }
    dialect = session.get_bind().dialect.name
    insert = pg_insert(TaskPlannerWakeState) if dialect == "postgresql" else sqlite_insert(TaskPlannerWakeState)
    session.execute(insert.values(**values).on_conflict_do_nothing(
        index_elements=["tenant_id", "task_id"]
    ))
    state = _locked_wake_state(session, task)
    if state is None:
        raise RuntimeError("planner_wake_state_unavailable")
    return state


def _earliest_not_before(
    current: datetime | None,
    proposed: datetime,
) -> datetime:
    if current is None:
        return as_beijing(proposed)
    return min(as_beijing(current), as_beijing(proposed))


__all__ = [
    "complete_task_planner_wake",
    "mark_task_planner_started",
    "wake_task_planner",
]
