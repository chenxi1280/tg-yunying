"""Group content admission must not wait behind a listener writing its Task."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TaskPlannerWakeState, TgGroup
from app.services._common import _now
from app.services.task_center.ai_group_content_allocation import _lock_group_surface
from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
from app.services.task_center.listener_runtime import _mark_listener_runtime_success
from app.services.task_center.task_retirement import lock_task_for_planning
from tests.test_execution_reference_locks_postgres import GROUP_ID, WAIT_SECONDS, _seed_group
from tests.test_runtime_retention_protection_postgres import database

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def _listener_write(database, group_locked):
    with Session(database, autoflush=False) as session:
        group = session.get(TgGroup, GROUP_ID)
        group.listener_last_polled_at = _now()
        session.flush()
        group_locked.set()
        _mark_listener_runtime_success(session, task_ids=["qa-task"], group_id=GROUP_ID,
            inserted=1, occurred_at=_now())
        session.commit()


def test_busy_content_surface_releases_planning_for_listener_and_preserves_wake(database):
    _seed_group(database)
    group_locked = Event()
    with Session(database, autoflush=False) as planner, ThreadPoolExecutor(max_workers=1) as executor:
        task = lock_task_for_planning(planner, "qa-task")
        assert task is not None
        writer = executor.submit(_listener_write, database, group_locked)
        assert group_locked.wait(timeout=WAIT_SECONDS)
        try:
            with pytest.raises(RuntimeResourceBlocked) as caught:
                _lock_group_surface(planner, GROUP_ID)
            assert caught.value.code == "ai_group_surface_busy"
        finally:
            planner.rollback()
            writer.result(timeout=WAIT_SECONDS)
        task = lock_task_for_planning(planner, "qa-task")
        _lock_group_surface(planner, GROUP_ID)
        assert task.stats["listener_runtime_last_collect_count"] == 1
        wake = planner.scalar(select(TaskPlannerWakeState))
        assert wake.wake_revision == 1 and wake.planned_revision == 0
        assert wake.planning_revision == 0


def test_missing_content_surface_keeps_explicit_error(database):
    _seed_group(database)
    with Session(database) as session:
        with pytest.raises(ValueError, match="ai_group_surface_group_missing"):
            _lock_group_surface(session, GROUP_ID + 1)
