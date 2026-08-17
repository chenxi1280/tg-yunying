from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Task, TaskPlannerWakeState, Tenant
from app.services.task_center.planner_wake import (
    complete_task_planner_wake,
    mark_task_planner_started,
    wake_task_planner,
)
from app.services.task_center.service import _normal_planner_task_ids


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 8, 17, 14, 0)


def test_wake_projection_controls_due_selection_without_polling_all_tasks() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant"))
        future = Task(
            id="future-task",
            tenant_id=1,
            name="future",
            type="channel_view",
            status="running",
            next_run_at=NOW - timedelta(hours=1),
        )
        legacy = Task(
            id="legacy-task",
            tenant_id=1,
            name="legacy",
            type="channel_view",
            status="running",
            next_run_at=NOW - timedelta(seconds=1),
        )
        session.add_all([future, legacy])
        session.flush()
        wake_task_planner(
            session,
            future,
            reason_code="future_snapshot_probe",
            not_before_at=NOW + timedelta(minutes=5),
        )
        session.commit()

        assert _normal_planner_task_ids(session, limit=10, now=NOW) == ["legacy-task"]
        wake_task_planner(
            session,
            future,
            reason_code="listener_snapshot_ready",
            not_before_at=NOW,
        )
        session.flush()
        assert set(_normal_planner_task_ids(session, limit=10, now=NOW)) == {
            "legacy-task",
            "future-task",
        }


def test_completed_wake_moves_time_due_without_losing_revision() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant"))
        task = Task(
            id="wake-task",
            tenant_id=1,
            name="wake",
            type="channel_view",
            status="running",
        )
        session.add(task)
        session.flush()
        state = wake_task_planner(
            session,
            task,
            reason_code="task_started",
            not_before_at=NOW,
        )
        revision = state.wake_revision
        complete_task_planner_wake(
            session,
            task,
            next_run_at=NOW + timedelta(minutes=1),
        )
        session.flush()
        reloaded = session.get(TaskPlannerWakeState, state.id)

        assert reloaded.planned_revision == revision
        assert reloaded.not_before_at == NOW + timedelta(minutes=1)


def test_listener_wake_during_planning_remains_due_after_completion() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant"))
        task = Task(
            id="concurrent-wake-task",
            tenant_id=1,
            name="concurrent wake",
            type="channel_view",
            status="running",
        )
        session.add(task)
        session.flush()
        wake_task_planner(session, task, reason_code="task_started", not_before_at=NOW)
        mark_task_planner_started(session, task)
        wake_task_planner(
            session,
            task,
            reason_code="listener_snapshot_ready",
            not_before_at=NOW,
        )

        complete_task_planner_wake(
            session,
            task,
            next_run_at=NOW + timedelta(minutes=5),
        )
        state = session.scalar(select(TaskPlannerWakeState))

        assert state.planned_revision == 1
        assert state.wake_revision == 2
        assert state.not_before_at == NOW
