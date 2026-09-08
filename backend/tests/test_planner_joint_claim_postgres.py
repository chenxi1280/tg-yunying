"""A busy wake must not leave the planner holding the Task needed by its writer."""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Task, TaskPlannerWakeState
from app.services.task_center.planner_wake import (
    complete_task_planner_wake, mark_task_planner_started, wake_task_planner,
)
from app.services.task_center.service import _prepare_task_planning_transaction
from app.services.task_center.task_retirement import lock_task_for_planning
from tests.test_engagement_assignment_postgres import _seed
from tests.test_runtime_retention_protection_postgres import database

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def _seed_tasks(database):
    with Session(database) as session:
        task = _seed(session)
        task.type = "group_ai_chat"
        healthy = Task(id="healthy-task", tenant_id=1, name="healthy", type="channel_view", status="running")
        session.add(healthy)
        wake_task_planner(session, task, reason_code="pending")
        wake_task_planner(session, healthy, reason_code="pending")
        session.commit()


def _hold_wake(session):
    return session.scalar(select(TaskPlannerWakeState).where(
        TaskPlannerWakeState.task_id == "qa-task").with_for_update())


def test_busy_wake_releases_task_lock_and_healthy_planning_continues(database):
    _seed_tasks(database)
    with Session(database) as writer, Session(database) as planner:
        wake = _hold_wake(writer)
        assert lock_task_for_planning(planner, "qa-task") is None
        task = writer.scalar(select(Task).where(Task.id == "qa-task").with_for_update(nowait=True))
        task.last_error = "writer still owns pending wake"
        writer.flush()
        assert wake.wake_revision == 1 and wake.planned_revision == 0
        healthy = lock_task_for_planning(planner, "healthy-task")
        assert healthy is not None
        mark_task_planner_started(planner, healthy)
        complete_task_planner_wake(planner, healthy, next_run_at=None)
        planner.commit()
        writer.commit()
        restored = lock_task_for_planning(planner, "qa-task")
        mark_task_planner_started(planner, restored)
        complete_task_planner_wake(planner, restored, next_run_at=None)
        planner.commit()
        state = planner.scalar(select(TaskPlannerWakeState).where(TaskPlannerWakeState.task_id == "qa-task"))
        assert state.wake_revision == state.planned_revision == 1
        assert state.planning_revision == 0


def test_ai_commit_boundary_yields_when_wake_is_busy_and_releases_task(database):
    _seed_tasks(database)
    with Session(database) as writer, Session(database) as planner:
        _hold_wake(writer)
        task = planner.get(Task, "qa-task")
        reloaded, processed, has_open, future = _prepare_task_planning_transaction(planner, task)
        assert reloaded is None and (processed, has_open, future) == (0, False, False)
        assert writer.scalar(select(Task.id).where(Task.id == "qa-task").with_for_update(nowait=True)) == "qa-task"


def test_task_without_wake_keeps_original_bootstrap_contract(database):
    with Session(database) as session:
        _seed(session)
        session.commit()
        task = lock_task_for_planning(session, "qa-task")
        assert task is not None
        mark_task_planner_started(session, task)
        complete_task_planner_wake(session, task, next_run_at=None)
        session.commit()
        wake = session.scalar(select(TaskPlannerWakeState))
        assert wake.wake_revision == wake.planned_revision == 1


def test_joint_claim_refreshes_a_cached_wake_before_acknowledging_latest_revision(database):
    _seed_tasks(database)
    with Session(database, expire_on_commit=False) as planner:
        cached = planner.scalar(select(TaskPlannerWakeState).where(TaskPlannerWakeState.task_id == "qa-task"))
        assert cached.wake_revision == 1
        planner.commit()
        with Session(database) as producer:
            wake_task_planner(producer, producer.get(Task, "qa-task"), reason_code="new_event")
            producer.commit()
        task = lock_task_for_planning(planner, "qa-task")
        mark_task_planner_started(planner, task)
        complete_task_planner_wake(planner, task, next_run_at=None)
        planner.commit()
        assert cached.wake_revision == cached.planned_revision == 2
