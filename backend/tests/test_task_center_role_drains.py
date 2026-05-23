from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import AccountStatus, Action, RuntimeMetricSnapshot, Task, TaskRuntimeSummary, Tenant, TgAccount, WorkerHeartbeat
from app.services._common import _now
from app.services.task_center import service


def _session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def test_role_drains_record_distinct_heartbeats(monkeypatch):
    SessionFactory = _session_factory()
    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

    monkeypatch.setattr(service, "drain_listener_runtime", lambda *_args, **_kwargs: type("Result", (), {"source_count": 0, "processed_count": 0})())

    assert service.drain_task_listener(SessionFactory, 5) == 0
    assert service.drain_task_recovery(SessionFactory, 5) >= 0
    assert service.drain_task_planner(SessionFactory, 5) >= 0
    assert service.drain_task_metrics(SessionFactory, 5) >= 1

    with SessionFactory() as session:
        roles = set(session.scalars(select(WorkerHeartbeat.process_type)))
        metric_count = session.query(RuntimeMetricSnapshot).count()

    assert {"listener", "recovery", "planner", "metrics"}.issubset(roles)
    assert metric_count >= 1


def test_metrics_drain_does_not_rebuild_all_runtime_summaries() -> None:
    SessionFactory = _session_factory()
    now_value = _now()
    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-unrelated-summary", tenant_id=1, name="不应被全量汇总", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-unrelated-summary",
                tenant_id=1,
                task_id="task-unrelated-summary",
                task_type="group_ai_chat",
                action_type="send_message",
                status="failed",
                scheduled_at=now_value,
                executed_at=now_value,
                result={"failure_type": "send_failed"},
            )
        )
        session.commit()

    assert service.drain_task_metrics(SessionFactory, 5) >= 1

    with SessionFactory() as session:
        assert session.scalar(select(TaskRuntimeSummary).where(TaskRuntimeSummary.task_id == "task-unrelated-summary")) is None


def test_dispatcher_role_claims_and_dispatches_without_listener(monkeypatch):
    SessionFactory = _session_factory()
    now_value = _now()
    listener_called = False

    def fake_listener(*_args, **_kwargs):
        nonlocal listener_called
        listener_called = True
        return type("Result", (), {"source_count": 0, "processed_count": 0})()

    def fake_dispatch(session: Session, action: Action) -> bool:
        action.status = "success"
        action.executed_at = _now()
        action.result = {"success": True, "remote_message_id": "mock-role-drain"}
        return True

    monkeypatch.setattr(service, "drain_listener_runtime", fake_listener)
    monkeypatch.setattr(service, "dispatch_action", fake_dispatch)

    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="A", phone_masked="***", status=AccountStatus.ACTIVE.value, session_ciphertext="mock"))
        session.add(Task(id="task-dispatcher-role", tenant_id=1, name="dispatcher", type="group_relay", status="running"))
        session.add(
            Action(
                id="action-dispatcher-role",
                tenant_id=1,
                task_id="task-dispatcher-role",
                task_type="group_relay",
                action_type="send_message",
                account_id=11,
                status="pending",
                scheduled_at=now_value - timedelta(seconds=1),
                payload={"chat_id": "-1001", "message_text": "hello"},
            )
        )
        session.commit()

    assert service.drain_task_dispatcher(SessionFactory, 5) == 1
    assert listener_called is False

    with SessionFactory() as session:
        action = session.get(Action, "action-dispatcher-role")
        heartbeat = session.scalar(select(WorkerHeartbeat).where(WorkerHeartbeat.process_type == "dispatcher"))

    assert action.status == "success"
    assert action.result["remote_message_id"] == "mock-role-drain"
    assert heartbeat is not None
