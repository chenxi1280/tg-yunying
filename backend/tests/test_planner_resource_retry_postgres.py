"""Planner contention classification against a real PostgreSQL row lock."""
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.timezone import as_beijing
from app.models import Task, TaskPlannerWakeState, TgGroup
from app.services._common import _now
from app.services.task_center import service
from app.services.task_center.ai_group_content_allocation import _lock_group_surface
from app.services.task_center.engagement_runtime_error import (
    DEFAULT_RESOURCE_RETRY_SECONDS,
    RuntimeResourceBlocked,
)
from app.services.task_center.planner_resource_retry import record_planner_resource_retry
from test_fulfillment_planner_isolation import _add_planner_tasks

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]
BROKEN = "task-planner-broken"
READY = "task-planner-ready"
RETRY_SECONDS = 73


@pytest.fixture
def factory(postgres_test_session_lock):
    url = make_url(os.environ["TEST_DATABASE_URL"])
    assert url.database == "tg_yunying_test"
    schema = "planner_retry_" + uuid4().hex
    admin = create_engine(url)
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={
            "options": f"-csearch_path={schema} -clock_timeout=1000 -ctimezone=Asia/Shanghai"})
        try:
            Base.metadata.create_all(engine)
            result = sessionmaker(bind=engine)
            _add_planner_tasks(result)
            with result.begin() as session:
                session.add(TgGroup(id=1, tenant_id=1, tg_peer_id="-1001", title="test"))
            yield result
        finally:
            engine.dispose()
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def test_row_contention_rolls_back_one_task_and_continues(factory, monkeypatch):
    monkeypatch.setattr(service, "_normal_planner_task_ids", lambda *a, **k: [BROKEN, READY])
    monkeypatch.setattr(service, "planner_global_pending", lambda *a: 0)

    def plan(_factory, task_id, *_args, **kwargs):
        with _factory.begin() as session:
            task = session.get(Task, task_id)
            task.name = "planned"
            session.flush()
            if task_id == BROKEN:
                _lock_group_surface(session, 1)
        return 2, False, kwargs["global_pending"]

    monkeypatch.setattr(service, "_plan_due_task", plan)
    before = _now()
    with factory.begin() as holder:
        holder.scalar(select(TgGroup).where(TgGroup.id == 1).with_for_update())
        processed, _ = service._drain_task_planner(factory, limit=5, process_type=None)
    assert processed == 2
    with factory() as session:
        task = session.get(Task, BROKEN)
        assert task.status == "running"
        assert task.name == BROKEN
        assert task.stats["planner_resource_busy"]["code"] == "ai_group_surface_busy"
        assert "planner_runtime_error" not in task.stats
        assert as_beijing(task.next_run_at) >= before + timedelta(seconds=DEFAULT_RESOURCE_RETRY_SECONDS)
        assert session.get(Task, READY).name == "planned"
        wake = session.scalar(select(TaskPlannerWakeState).where(TaskPlannerWakeState.task_id == BROKEN))
        assert wake.not_before_at == task.next_run_at
        assert wake.planning_revision == 0


def test_retry_preserves_full_detail_and_exact_delay(factory, monkeypatch):
    now = _now()
    monkeypatch.setattr("app.services.task_center.planner_resource_retry._now", lambda: now)
    detail = "resource contention " * 40
    record_planner_resource_retry(factory, BROKEN, RuntimeResourceBlocked("busy", detail, RETRY_SECONDS))
    with factory() as session:
        task = session.get(Task, BROKEN)
        assert task.stats["planner_resource_busy"]["detail"] == detail
        assert as_beijing(task.next_run_at) == now + timedelta(seconds=RETRY_SECONDS)


@pytest.mark.parametrize("status", ["paused", "stopped"])
def test_retry_cannot_restart_inactive_task(factory, status):
    with factory.begin() as session:
        task = session.get(Task, BROKEN)
        task.status = status
        original_next_run = task.next_run_at
    record_planner_resource_retry(factory, BROKEN, RuntimeResourceBlocked("busy", "locked"))
    with factory() as session:
        task = session.get(Task, BROKEN)
        assert task.status == status
        assert task.next_run_at == original_next_run
        assert "planner_resource_busy" not in (task.stats or {})
