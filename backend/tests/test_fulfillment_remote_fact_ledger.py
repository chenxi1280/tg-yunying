from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ExecutionAttempt,
    FulfillmentObligationProjection,
    OperationTarget,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    Tenant,
)
from app.services.task_center.fulfillment_remote_facts import (
    ensure_action_obligation,
    persist_remote_fact,
)


NOW = datetime(2026, 8, 18, 10, 0)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=980_001, name="tenant"))
        current.add(OperationTarget(
            id=980_020,
            tenant_id=980_001,
            target_type="group",
            tg_peer_id="-100980020",
            title="group",
        ))
        current.commit()
        yield current


@pytest.mark.no_postgres
def test_ai_fact_uses_primary_quantity_owner_ledger(session: Session) -> None:
    action = _add_ai_owner_and_action(session)
    assert ensure_action_obligation(session, action)
    projection = session.scalar(select(FulfillmentObligationProjection))
    assert projection.task_day_ledger_id == "ledger-ai"
    projection.task_day_ledger_id = None

    action.status = "success"
    attempt = ExecutionAttempt(
        tenant_id=980_001,
        action_id=action.id,
        attempt_no=1,
        status="success",
        gateway_call_started_at=NOW,
        after_call_at=NOW + timedelta(seconds=1),
        remote_message_id="remote-1",
    )
    session.add(attempt)
    session.flush()
    fact = persist_remote_fact(session, action)
    assert fact is not None
    assert fact.task_day_ledger_id == "ledger-ai"
    assert projection.task_day_ledger_id == "ledger-ai"


@pytest.mark.no_postgres
def test_ai_fact_rejects_payload_owner_ledger_conflict(session: Session) -> None:
    action = _add_ai_owner_and_action(session)
    action.payload = {**action.payload, "task_day_ledger_id": "ledger-other"}
    with pytest.raises(ValueError, match="fulfillment_ledger_identity_conflict"):
        ensure_action_obligation(session, action)


@pytest.mark.no_postgres
def test_ai_obligation_uses_quantity_owner_over_legacy_coverage(session: Session) -> None:
    action = _add_ai_owner_and_action(session)
    action.obligation_type = "coverage"
    action.obligation_id = "coverage-reused"
    action.payload = {
        **action.payload,
        "coverage_ledger_id": "coverage-reused",
    }
    session.add(FulfillmentObligationProjection(
        id="projection-legacy-coverage",
        tenant_id=980_001,
        task_id=action.task_id,
        task_day_ledger_id="ledger-other",
        task_lifecycle_epoch=1,
        obligation_type="coverage",
        obligation_id="coverage-reused",
        work_lane="interaction",
        state="open",
        active_action_id=action.id,
        materialization_version=1,
        version=1,
    ))
    session.flush()

    assert ensure_action_obligation(session, action)
    assert (action.obligation_type, action.obligation_id) == (
        "quantity_slot",
        "quantity-ai",
    )
    projection = session.scalar(select(FulfillmentObligationProjection).where(
        FulfillmentObligationProjection.obligation_type == "quantity_slot",
    ))
    assert projection.task_day_ledger_id == "ledger-ai"


@pytest.mark.no_postgres
def test_current_ai_fact_rejects_missing_ledger_owner(session: Session) -> None:
    action = _add_ai_owner_and_action(session)
    action.primary_quantity_slot_id = None
    action.payload = {}
    assert ensure_action_obligation(session, action)
    action.status = "success"
    session.add(ExecutionAttempt(
        tenant_id=980_001,
        action_id=action.id,
        attempt_no=1,
        status="success",
        gateway_call_started_at=NOW,
        after_call_at=NOW + timedelta(seconds=1),
        remote_message_id="remote-missing-ledger",
    ))
    session.flush()
    with pytest.raises(ValueError, match="fulfillment_ai_ledger_missing"):
        persist_remote_fact(session, action)


def _add_ai_owner_and_action(session: Session) -> Action:
    task = Task(
        id="task-ai",
        tenant_id=980_001,
        name="task",
        type="group_ai_chat",
        status="running",
        fulfillment_contract_version="fact_first_v3",
    )
    ledger = TaskDayLedger(
        id="ledger-ai",
        tenant_id=980_001,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 8, 18),
        period_start_at=NOW,
        deadline_at=NOW + timedelta(days=1),
        day_phase="active",
        planning_anchor_at=NOW,
    )
    quantity = TaskGroupDailyMessageSlot(
        id="quantity-ai",
        tenant_id=980_001,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        target_operation_target_id=980_020,
        slot_kind="quantity",
        slot_ordinal=1,
    )
    action = Action(
        id="action-ai",
        tenant_id=980_001,
        task_id=task.id,
        task_type="group_ai_chat",
        action_type="send_message",
        primary_quantity_slot_id=quantity.id,
        status="claiming",
        payload={"primary_quantity_slot_id": quantity.id},
    )
    session.add_all([task, ledger, quantity, action])
    session.flush()
    return action
