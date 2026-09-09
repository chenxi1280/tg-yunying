"""Clone boundary reads belong to listener, under the real runtime IO guard."""
from datetime import timedelta
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from app.models import Task, TaskPlannerWakeState
from app.models.group_clone import CloneSourceStreamState
from app.schemas.task_center import GroupCloneTaskCreate
from app.services._common import _now
from app.services.task_center import group_clone_source_stream, service
from app.services.task_center.group_clone_lifecycle import create_and_start_group_clone_task
from app.telethon_lifecycle import TelethonClientLifecycle
import test_group_clone_api as api_tests
from test_group_clone_lifecycle import _create_payload

pytestmark = pytest.mark.no_postgres
client_and_session = api_tests.client_and_session


def _pending(session):
    task, _ = create_and_start_group_clone_task(
        session, 1, 1, payload=GroupCloneTaskCreate.model_validate(_create_payload()),
    )
    session.commit()
    return task.id


def _boundary(monkeypatch, calls):
    def fetch(*args, **kwargs):
        TelethonClientLifecycle._assert_remote_io_allowed()
        calls.append(args[0])
        return {"channel_pts": 100, "max_message_id": 10}
    monkeypatch.setattr(group_clone_source_stream.gateway, "fetch_raw_channel_boundary", fetch)


def test_planner_leaves_clone_pending_without_remote_io(client_and_session, monkeypatch):
    _, session = client_and_session
    task_id = _pending(session)
    calls = []
    _boundary(monkeypatch, calls)
    monkeypatch.setattr(TelethonClientLifecycle, "_runtime_role", "planner")
    service._activate_pending_tasks(session)
    assert session.get(Task, task_id).status == "pending"
    assert calls == []


def test_listener_commits_boundary_and_wake_once(client_and_session, monkeypatch):
    from app.services.task_center.group_clone_start_worker import advance_pending_group_clones
    _, session = client_and_session
    task_id = _pending(session)
    calls = []
    _boundary(monkeypatch, calls)
    monkeypatch.setattr(TelethonClientLifecycle, "_runtime_role", "listener")
    factory = sessionmaker(bind=session.get_bind())
    assert advance_pending_group_clones(factory, tenant_id=1, limit=10) == 1
    assert advance_pending_group_clones(factory, tenant_id=1, limit=10) == 0
    session.expire_all()
    task = session.get(Task, task_id)
    stream = session.scalar(select(CloneSourceStreamState).where(CloneSourceStreamState.task_id == task_id))
    wake = session.scalar(select(TaskPlannerWakeState).where(TaskPlannerWakeState.task_id == task_id))
    assert task.status == "running" and stream.start_message_id == 10
    assert stream.start_pts == 100 and stream.state == "live"
    assert wake.reason_code == "task_activated" and wake.wake_revision == 1
    assert calls == ["-100111"]


@pytest.mark.parametrize("condition", ["paused", "future", "deleted", "other_tenant"])
def test_listener_respects_start_scope(client_and_session, monkeypatch, condition):
    from app.services.task_center.group_clone_start_worker import advance_pending_group_clones
    _, session = client_and_session
    task_id = _pending(session)
    task = session.get(Task, task_id)
    if condition == "paused": task.status = "paused"
    if condition == "future": task.scheduled_start = _now() + timedelta(hours=1)
    if condition == "deleted": task.deleted_at = _now()
    session.commit()
    calls = []
    _boundary(monkeypatch, calls)
    tenant_id = 2 if condition == "other_tenant" else 1
    assert advance_pending_group_clones(sessionmaker(bind=session.get_bind()), tenant_id=tenant_id, limit=10) == 0
    assert calls == []


def test_listener_persists_remote_start_failure(client_and_session, monkeypatch):
    from app.services.task_center.group_clone_start_worker import advance_pending_group_clones
    _, session = client_and_session
    task_id = _pending(session)
    def fail(*args, **kwargs):
        raise RuntimeError("boundary_read_failed")
    monkeypatch.setattr(group_clone_source_stream.gateway, "fetch_raw_channel_boundary", fail)
    assert advance_pending_group_clones(sessionmaker(bind=session.get_bind()), tenant_id=1, limit=10) == 1
    session.expire_all()
    task = session.get(Task, task_id)
    assert task.status == "failed" and task.last_error == "boundary_read_failed"
    assert session.scalar(select(TaskPlannerWakeState).where(TaskPlannerWakeState.task_id == task_id)) is None


def test_normal_listener_starts_clone_after_collector(client_and_session, monkeypatch):
    from app.services.task_center import listener_runtime
    from app.services.task_center.telegram_update_collector import CollectorDrainResult
    _, session = client_and_session
    task_id = _pending(session)
    stages = []
    def collector(*args, **kwargs):
        stages.append("collector")
        return CollectorDrainResult()
    def boundary(*args, **kwargs):
        TelethonClientLifecycle._assert_remote_io_allowed()
        assert stages == ["collector"]
        stages.append("boundary")
        return {"channel_pts": 100, "max_message_id": 10}
    monkeypatch.setattr(TelethonClientLifecycle, "_runtime_role", "listener")
    monkeypatch.setattr(listener_runtime, "drain_telegram_update_collector", collector)
    monkeypatch.setattr(group_clone_source_stream.gateway, "fetch_raw_channel_boundary", boundary)
    result = listener_runtime.drain_listener_runtime(sessionmaker(bind=session.get_bind()), tenant_id=1, limit=10)
    session.expire_all()
    assert session.get(Task, task_id).status == "running"
    assert stages == ["collector", "boundary"]
    assert result.collected_count == 1
