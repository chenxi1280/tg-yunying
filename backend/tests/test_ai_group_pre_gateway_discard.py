"""An uncalled blocked speaker must release its slot for other speakers."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action, ExecutionAttempt, FulfillmentObligationProjection, FulfillmentRemoteFact,
    Task, TaskAccountDailyCoverage, TaskDayLedger, Tenant,
)
from app.services.task_center import dispatcher
from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked

pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 9, 1)


@pytest.fixture
def rows(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dispatcher, "_now", lambda: NOW)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="test"))
        session.add(Task(id="task", tenant_id=1, name="test", type="group_ai_chat",
            status="running", fulfillment_contract_version="fact_first_v3",
            type_config={"engagement_contract_version": "unified_engagement_v1"}))
        action = Action(id="action", tenant_id=1, task_id="task", account_id=1,
            task_type="group_ai_chat", action_type="send_message", status="running",
            scheduled_at=NOW, payload={"coverage_ledger_id": "coverage"})
        attempt = ExecutionAttempt(id="attempt", tenant_id=1, action_id="action",
            account_id=1, status="before_call", attempt_no=2)
        coverage = TaskAccountDailyCoverage(id="coverage", tenant_id=1, task_id="task",
            group_id=1, account_id=1, coverage_date=NOW.date(), target_count=1,
            state="reserved", reserved_action_id="action", targeted_at=NOW)
        session.add_all([action, attempt, coverage])
        session.commit()
        yield session, action, attempt, coverage
    engine.dispose()


@pytest.mark.parametrize("code", ["execution_circuit_open", "execution_circuit_half_open",
    "execution_circuit_probe_pending", "account_shared_usage_unproven", "account_legacy_remote_inflight"])
def test_blocked_uncalled_action_is_skipped_and_coverage_released(rows, code):
    session, action, attempt, coverage = rows
    dispatcher._defer_engagement_resource_attempt(action, attempt, RuntimeResourceBlocked(code, "test"))
    session.flush()
    dispatcher._sync_action_coverage_state(session, action)
    session.refresh(coverage)
    assert action.status == "skipped"
    assert action.result["remote_mutation_started"] is False
    assert action.result["pre_gateway_discarded"] is True
    assert attempt.status == "skipped_before_gateway"
    assert attempt.result_snapshot["remote_mutation_started"] is False
    assert coverage.state == "ready"
    assert coverage.reserved_action_id is None
    assert coverage.confirmed_count == 0
    assert coverage.next_eligible_at > NOW


def test_old_unknown_attempt_cannot_be_discarded(rows):
    session, action, attempt, coverage = rows
    session.add(ExecutionAttempt(id="unknown", tenant_id=1, action_id=action.id,
        attempt_no=1, status="result_unknown", gateway_call_started_at=NOW))
    session.flush()
    dispatcher._defer_engagement_resource_attempt(action, attempt,
        RuntimeResourceBlocked("execution_circuit_open", "test"))
    assert action.status == "pending"
    assert coverage.reserved_action_id == action.id
    assert session.get(ExecutionAttempt, "unknown").status == "result_unknown"


def test_normal_pacing_wait_is_not_discarded(rows):
    _session, action, attempt, coverage = rows
    dispatcher._defer_engagement_resource_attempt(action, attempt,
        RuntimeResourceBlocked("pacing_source_not_before", "test"))
    assert action.status == "pending"
    assert coverage.reserved_action_id == action.id


def test_finalizer_records_nonexecution_and_keeps_obligation_open(rows):
    from app.services.task_center.fulfillment_remote_facts import ensure_action_obligation

    session, action, attempt, coverage = rows
    ledger = TaskDayLedger(id="day", tenant_id=1, task_id="task", timezone_snapshot="Asia/Shanghai",
        timezone_revision=1, day_phase="full_day", planning_anchor_at=NOW,
        obligation_local_date=NOW.date(), period_start_at=NOW, deadline_at=NOW)
    session.add(ledger)
    action.payload = {**action.payload, "task_day_ledger_id": ledger.id}
    session.flush()
    assert ensure_action_obligation(session, action)
    dispatcher._defer_engagement_resource_attempt(action, attempt,
        RuntimeResourceBlocked("execution_circuit_open", "test"))
    dispatcher._finalize_dispatch_action(session, action)
    session.refresh(coverage)
    fact = session.query(FulfillmentRemoteFact).one()
    projection = session.query(FulfillmentObligationProjection).one()
    assert fact.fact_kind == "safely_not_executed"
    assert projection.state == "open"
    assert coverage.next_eligible_at > NOW
    assert coverage.confirmed_count == 0
