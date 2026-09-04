from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Action, ListenerSourceState, Task, TaskDayLedgerLifecycleEvent, TaskSourceSubscription, Tenant
from app.services.task_center.daily_ledgers import ensure_task_day_ledger
from app.timezone import BEIJING_TZ


NOW = datetime(2026, 9, 4, 12, tzinfo=BEIJING_TZ)


def _seed(session, mode):
    tenant_id = 990222 + ["continuous_event_driven", "finite_existing_sources", "promised_daily_sources"].index(mode)
    session.add(Tenant(id=tenant_id, name=f"source day QA {tenant_id}"))
    session.flush()
    task = Task(id=f"source-day-{tenant_id}", tenant_id=tenant_id, name="source day", type="channel_like",
        status="running", timezone="Asia/Shanghai", type_config={
            "engagement_contract_version": "unified_engagement_v1", "target_channel_id": 1,
            "source_expectation_mode": mode})
    session.add(task)
    session.flush()
    ledger = ensure_task_day_ledger(session, task, now=NOW)
    session.commit()
    return task, ledger


@pytest.mark.parametrize("mode,closed", [
    ("continuous_event_driven", "neutral_no_opportunity"),
    ("finite_existing_sources", "missed_no_source"),
    ("promised_daily_sources", "missed_promised_source"),
])
def test_unknown_source_day_closes_and_late_proof_revises_without_replay(mode, closed):
    with SessionLocal() as session:
        task, first = _seed(session, mode)
        next_day = NOW + timedelta(days=1)
        second = ensure_task_day_ledger(session, task, now=next_day)
        session.commit()
        assert first.lifecycle_status == "closed_source_ingestion_unproven"
        assert second.id != first.id
        _complete_source(session, task)
        session.commit()
        task.type_config = {**task.type_config, "source_expectation_mode": "finite_existing_sources"}
        ensure_task_day_ledger(session, task, now=next_day)
        session.commit()
        assert first.lifecycle_status == f"closed_{closed}"
        events = list(session.scalars(select(TaskDayLedgerLifecycleEvent.event_type).where(
            TaskDayLedgerLifecycleEvent.task_day_ledger_id == first.id)))
        assert "deadline_source_unproven" in events
        assert f"deadline_{closed}" in events
        assert session.scalar(select(Action.id).where(Action.task_id == task.id)) is None
        ensure_task_day_ledger(session, task, now=next_day)
        session.commit()
        assert len(list(session.scalars(select(TaskDayLedgerLifecycleEvent.event_type).where(
            TaskDayLedgerLifecycleEvent.task_day_ledger_id == first.id)))) == len(events)


def _complete_source(session, task):
    state = ListenerSourceState(id=f"source-day-observer-{task.tenant_id}", tenant_id=task.tenant_id,
        source_type="channel", source_peer_id="1", last_event_at=NOW.replace(hour=0),
        observed_at=NOW+timedelta(days=1))
    session.add(state)
    session.flush()
    session.add(TaskSourceSubscription(tenant_id=task.tenant_id, task_id=task.id,
        lifecycle_epoch=task.task_lifecycle_epoch, source_type="channel", source_peer_hash="source-day",
        listener_source_state_id=state.id))
