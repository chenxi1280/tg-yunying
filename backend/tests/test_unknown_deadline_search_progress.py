from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ExecutionAttempt,
    FulfillmentFactProjectionState,
    FulfillmentObligationProjection,
    FulfillmentRemoteFact,
    OperationTarget,
    SearchClickAssignment,
    SearchClickFulfillmentObligation,
    Task,
    TaskDayLedger,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
)
from app.services.task_center.executors.search_click_direct import _open_units
from app.services.task_center.dispatch_activation_ledger import (
    recover_fenced_dispatch_actions,
)
from app.services.task_center.unknown_deadline_closure import (
    close_unknown_after_deadline,
)


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        yield current


def _runtime(session: Session) -> tuple[Task, TaskDayLedger, list[SearchClickFulfillmentObligation]]:
    now = datetime(2026, 8, 4, 8, 0)
    tenant = Tenant(id=1, name="search recovery")
    target = OperationTarget(
        id=1,
        tenant_id=1,
        tg_peer_id="target-peer",
        title="target",
    )
    task = Task(
        id="search-task",
        tenant_id=1,
        name="search-task",
        type="search_click",
        status="running",
        fulfillment_contract_version="fact_first_v3",
    )
    ledger = TaskDayLedger(
        id="search-ledger",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 8, 4),
        period_start_at=now - timedelta(hours=8),
        deadline_at=now + timedelta(hours=16),
        day_phase="full_day",
        planning_anchor_at=now,
    )
    obligations = [
        SearchClickFulfillmentObligation(
            id=f"obligation-{ordinal}",
            tenant_id=1,
            task_day_ledger_id=ledger.id,
            target_id=target.id,
            click_obligation_ordinal=ordinal,
            status="open",
        )
        for ordinal in (1, 2)
    ]
    account = TgAccount(
        id=1,
        tenant_id=1,
        display_name="account",
        phone_masked="***",
    )
    authorization = TgAccountAuthorization(
        id=1,
        tenant_id=1,
        account_id=account.id,
    )
    session.add_all([tenant, target, task, ledger, *obligations, account, authorization])
    session.flush()
    return task, ledger, obligations


def _unknown_assignment(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    obligation: SearchClickFulfillmentObligation,
) -> SearchClickAssignment:
    assignment = SearchClickAssignment(
        id="assignment-unknown",
        tenant_id=1,
        task_id=task.id,
        obligation_id=obligation.id,
        account_id=1,
        authorization_id=1,
        keyword_hash="a" * 64,
        proxy_route_id="proxy-1",
        protocol_sample_version="v1",
        resource_snapshot_hash="b" * 64,
        solver_input_hash="c" * 64,
        obligation_deadline_at=ledger.deadline_at,
        binding_version=1,
        state="gateway_unknown",
    )
    session.add(assignment)
    session.flush()
    return assignment


def test_unknown_assignment_does_not_starve_later_open_obligation(session: Session) -> None:
    task, ledger, obligations = _runtime(session)
    _unknown_assignment(session, task, ledger, obligations[0])

    units = _open_units(session, datetime(2026, 8, 4, 8, 5), 1)

    assert [unit.obligation_id for unit in units] == [obligations[1].id]


def test_release_fence_preserves_search_target_and_closes_assignment_lifecycle(
    session: Session,
) -> None:
    task, ledger, obligations = _runtime(session)
    obligation = obligations[0]
    assignment = _unknown_assignment(session, task, ledger, obligation)
    assignment.state = "executing"
    projection = FulfillmentObligationProjection(
        tenant_id=1,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        obligation_type="search_click",
        obligation_id=obligation.id,
        work_lane="search",
        state="action_bound",
        active_action_id="action-fenced",
    )
    action = Action(
        id="action-fenced",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        account_id=1,
        status="executing",
        obligation_type="search_click",
        obligation_id=obligation.id,
        lease_owner="old-search-worker",
        lease_expires_at=datetime(2026, 8, 4, 8, 30),
        payload={
            "search_click_assignment_id": assignment.id,
            "search_click_obligation_id": obligation.id,
            "task_day_ledger_id": ledger.id,
        },
    )
    attempt = ExecutionAttempt(
        id="attempt-fenced",
        tenant_id=1,
        action_id=action.id,
        account_id=1,
        status="gateway_call_started",
        gateway_call_started_at=datetime(2026, 8, 4, 8, 1),
    )
    original_target_id = obligation.target_id
    obligation.source_action_id = action.id
    assignment.action_id = action.id
    session.add_all([projection, action, attempt])
    session.flush()

    assert recover_fenced_dispatch_actions(session, actor="release-owner") == 1
    session.expire_all()

    assert session.get(Action, action.id).status == "unknown_after_send"
    assert session.get(SearchClickAssignment, assignment.id).state == "gateway_unknown"
    current = session.get(SearchClickFulfillmentObligation, obligation.id)
    assert current.status == "unknown_after_send"
    assert current.target_id == original_target_id
    assert current.source_action_id == action.id


def test_deadline_closure_uses_short_terminal_and_appends_decision_fact(session: Session) -> None:
    task, ledger, obligations = _runtime(session)
    obligation = obligations[0]
    assignment = _unknown_assignment(session, task, ledger, obligation)
    action = Action(
        id="action-unknown",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="search_join",
        account_id=1,
        status="unknown_after_send",
        obligation_type="search_click",
        obligation_id=obligation.id,
        unknown_deadline_at=datetime(2000, 1, 1),
        payload={
            "search_click_assignment_id": assignment.id,
            "search_click_obligation_id": obligation.id,
        },
    )
    attempt = ExecutionAttempt(
        id="attempt-unknown",
        tenant_id=1,
        action_id=action.id,
        account_id=1,
        status="result_unknown",
        gateway_call_started_at=datetime(2026, 8, 4, 8, 1),
        after_call_at=datetime(2026, 8, 4, 8, 2),
    )
    projection = FulfillmentObligationProjection(
        tenant_id=1,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        obligation_type="search_click",
        obligation_id=obligation.id,
        work_lane="search",
        state="remote_reconcile_only",
        active_action_id=action.id,
    )
    source_fact = FulfillmentRemoteFact(
        tenant_id=1,
        task_type=task.type,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        obligation_type="search_click",
        obligation_id=obligation.id,
        action_id=action.id,
        attempt_id=attempt.id,
        mutation_kind="search_join",
        remote_mutation_key_hash="d" * 64,
        gateway_request_hash="e" * 64,
        fact_kind="remote_outcome_unknown",
        fact_identity_hash="f" * 64,
        outcome={"action_status": "unknown_after_send"},
    )
    obligation.source_action_id = action.id
    assignment.action_id = action.id
    session.add_all([action, attempt, projection, source_fact])
    session.flush()

    assert close_unknown_after_deadline(session, limit=10) == 1
    session.flush()
    session.expire_all()

    assert session.get(Action, action.id).status == "closed_unknown"
    assert session.get(SearchClickAssignment, assignment.id).state == "closed_unknown"
    assert session.get(SearchClickFulfillmentObligation, obligation.id).status == "closed_unknown"
    assert session.get(FulfillmentObligationProjection, projection.id).state == "closed_with_unknown_shortfall"
    decision = session.scalar(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.action_id == action.id,
        FulfillmentRemoteFact.fact_kind == "unknown_deadline_closed",
    ))
    assert decision is not None
    states = list(session.scalars(select(FulfillmentFactProjectionState).where(
        FulfillmentFactProjectionState.fact_id == decision.fact_id,
    )))
    assert len(states) == 3
    assert {row.state for row in states} == {"projected"}
