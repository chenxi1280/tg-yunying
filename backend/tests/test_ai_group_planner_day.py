"""A later planning rejection must not roll a running task back to yesterday."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Action, ExecutionAttempt, OperationTarget, Task, TaskDayLedger, TaskGroupDailyMessageSlot, TaskGroupDailyTarget, Tenant, TgGroup
from app.services.task_center import service
from app.services.task_center.daily_ledgers import ensure_task_day_ledger
from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 9, 1)
TASK_ID = "group-day-independent"
DAILY_TARGET = 30


@pytest.fixture
def factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    result = sessionmaker(bind=engine, autoflush=False)
    with result() as session:
        seed_calendar_task(session)
        session.commit()
    yield result
    engine.dispose()


def seed_calendar_task(session):
    session.add(Tenant(id=1, name="test"))
    session.flush()
    session.add(TgGroup(id=1, tenant_id=1, tg_peer_id="-1001", title="test group"))
    session.add(OperationTarget(id=1, tenant_id=1, target_type="group",
        tg_peer_id="-1001", title="test group"))
    task = Task(id=TASK_ID, tenant_id=1, type="group_ai_chat", name="test task",
        status="running", next_run_at=NOW - timedelta(seconds=1),
        fulfillment_contract_version="fact_first_v3", type_config={
            "engagement_contract_version": "unified_engagement_v1",
            "target_group_id": 1, "target_operation_target_id": 1,
            "daily_message_target": DAILY_TARGET})
    session.add(task)
    session.flush()
    previous = ensure_task_day_ledger(session, task, now=NOW - timedelta(days=1))
    action = Action(id="old-unknown", tenant_id=1, task_id=TASK_ID,
        task_type="group_ai_chat", action_type="send_message", status="unknown_after_send",
        scheduled_at=NOW - timedelta(days=1), payload={"task_day_ledger_id": previous.id})
    session.add(action)
    session.flush()
    session.add(ExecutionAttempt(id="old-attempt", tenant_id=1, action_id=action.id,
        status="result_unknown", gateway_call_started_at=NOW - timedelta(days=1)))


def _isolate_body(monkeypatch, build):
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setattr(service, "retry_failed_actions", lambda *_a, **_kw: 0)
    monkeypatch.setattr(service, "_planning_backlog_blocked", lambda *_a: False)
    monkeypatch.setattr(service, "build_task_plan", build)


def _plan(factory):
    return service._plan_due_task(factory, TASK_ID, None, limit=20, global_pending=0)


def test_body_resource_failure_keeps_new_day_and_original_unknown(factory, monkeypatch):
    def reject(_session, _task):
        raise RuntimeResourceBlocked("ai_group_surface_busy", "test contention")
    _isolate_body(monkeypatch, reject)
    with pytest.raises(RuntimeResourceBlocked):
        _plan(factory)
    with factory() as session:
        ledgers = list(session.scalars(select(TaskDayLedger).where(
            TaskDayLedger.task_id == TASK_ID).order_by(TaskDayLedger.period_start_at)))
        assert len(ledgers) == 2
        assert ledgers[0].lifecycle_status != "open"
        assert ledgers[1].obligation_local_date == NOW.date()
        target = session.scalar(select(TaskGroupDailyTarget).where(
            TaskGroupDailyTarget.task_day_ledger_id == ledgers[1].id))
        assert target is not None
        assert session.query(TaskGroupDailyMessageSlot).filter_by(
            task_day_ledger_id=ledgers[1].id).count() == target.effective_message_target
        old = session.get(Action, "old-unknown")
        assert old.status == "unknown_after_send"
        assert old.payload["task_day_ledger_id"] == ledgers[0].id
        assert session.get(ExecutionAttempt, "old-attempt").status == "result_unknown"


def test_waiting_for_membership_still_initializes_day_once(factory, monkeypatch):
    _isolate_body(monkeypatch, lambda _s, _t: 0)
    _plan(factory)
    _plan(factory)
    with factory() as session:
        assert session.query(TaskDayLedger).filter_by(
            task_id=TASK_ID, obligation_local_date=NOW.date()).count() == 1
        assert session.query(TaskGroupDailyTarget).filter_by(
            task_id=TASK_ID, target_date=NOW.date()).count() == 1


@pytest.mark.parametrize("status", ["paused", "stopped"])
def test_nonrunning_task_does_not_initialize_new_day(factory, monkeypatch, status):
    with factory() as session:
        session.get(Task, TASK_ID).status = status
        session.commit()
    _isolate_body(monkeypatch, lambda _s, _t: pytest.fail("nonrunning task planned"))
    _plan(factory)
    with factory() as session:
        assert session.query(TaskDayLedger).filter_by(
            task_id=TASK_ID, obligation_local_date=NOW.date()).count() == 0
