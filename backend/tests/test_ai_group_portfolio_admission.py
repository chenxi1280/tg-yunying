"""Admission changes execution supply without changing the frozen demand."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import (
    AccountBehaviorBudgetPolicyRevision, AccountPortfolioLoadReservation,
    Task, TaskDayLedger,
)
from app.services.task_center import engagement_portfolio_recovery as recovery
from app.services.task_center.engagement_portfolio import reserve_portfolio_units
from app.services.task_center.executors import group_ai_chat
from tests.test_engagement_portfolio import DAY_START, _session

pytestmark = pytest.mark.no_postgres


@pytest.fixture
def admitted_portfolio(monkeypatch):
    monkeypatch.setattr(recovery, "_now", lambda: DAY_START + timedelta(hours=1))
    with _session() as session:
        policy = session.scalar(select(AccountBehaviorBudgetPolicyRevision))
        policy.action_budgets = {"total": 2, "authored_message": 2}
        task = session.get(Task, "task-a")
        task.type = "group_ai_chat"
        session.flush()
        yield session, task, session.get(TaskDayLedger, "ledger-a")


def _reserve(scope, admitted, units=3):
    session, task, ledger = scope
    return reserve_portfolio_units(session, task, ledger,
        action_class="authored_message", demand_identity="group_daily:target",
        total_units=units, candidate_account_ids=[11, 12],
        allocatable_account_ids=frozenset(admitted))


@pytest.mark.parametrize("admitted_account", [11, 12])
def test_daily_portfolio_entry_allocates_only_admitted_accounts(admitted_portfolio, admitted_account):
    session, task, ledger = admitted_portfolio
    ready = SimpleNamespace(account_id=admitted_account)

    rows = group_ai_chat._portfolio_coverage_rows(
        session, task, ledger=ledger,
        target=SimpleNamespace(id="target", effective_message_target=1),
        participation=SimpleNamespace(selected_account_ids=[11, 12]), rows=[ready],
        allocatable_account_ids=frozenset({admitted_account}),
    )

    assert rows == [ready]
    reservations = list(session.scalars(select(AccountPortfolioLoadReservation)))
    assert [(row.account_id, row.reserved_units) for row in reservations] == [(admitted_account, 1)]


def test_later_admission_recovers_original_deficit_without_changing_frozen_demand(
    admitted_portfolio,
):
    session, task, _ledger = admitted_portfolio
    first = _reserve(admitted_portfolio, {11})
    demand = first.plan.demand_snapshot
    assert first.allocated_units_by_account == {11: 2}
    assert first.deficit_units == 1

    waiting = _reserve(admitted_portfolio, {11})
    assert waiting.plan.id == first.plan.id
    assert waiting.allocated_units_by_account == {11: 2}

    joined = _reserve(admitted_portfolio, {11, 12})
    replay = _reserve(admitted_portfolio, {11, 12})
    assert joined.allocated_units_by_account == {11: 2, 12: 1}
    assert joined.plan.demand_snapshot == demand
    assert first.plan.state == "superseded"
    assert replay.plan.id == joined.plan.id
    assert joined.deficit_units == 0
    assert task.last_error != "portfolio_input_changed_after_freeze"
    assert len(list(session.scalars(select(AccountPortfolioLoadReservation)))) == 2


def test_empty_admission_freezes_obligation_without_reserving_budget(admitted_portfolio):
    session, _task, _ledger = admitted_portfolio
    pending = _reserve(admitted_portfolio, set())
    assert pending.requested_units == 3
    assert pending.allocated_units_by_account == {}
    assert list(session.scalars(select(AccountPortfolioLoadReservation))) == []

    joined = _reserve(admitted_portfolio, {12})
    assert joined.allocated_units_by_account == {12: 2}
    assert joined.plan.demand_snapshot == pending.plan.demand_snapshot


def test_admission_loss_preserves_original_allocated_budget(admitted_portfolio):
    session, _task, _ledger = admitted_portfolio
    original = _reserve(admitted_portfolio, {11}, units=1)
    changed = _reserve(admitted_portfolio, {12}, units=1)

    assert changed.plan.id == original.plan.id
    assert changed.allocated_units_by_account == {11: 1}
    reservations = list(session.scalars(select(AccountPortfolioLoadReservation)))
    assert [(row.account_id, row.reserved_units, row.state) for row in reservations] == [
        (11, 1, "active"),
    ]
