"""Recover the original unallocated demand without recreating consumed ownership."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    AccountBehaviorBudgetLedger, AccountBehaviorBudgetPolicyRevision,
    AccountPortfolioLoadReservation, PortfolioFeasibilityPlanRevision, Task, TaskDayLedger, TgAccount,
)
from app.services.task_center import engagement_portfolio_recovery as recovery
from app.services.task_center.engagement_portfolio import reserve_portfolio_units
from tests.test_engagement_portfolio import DAY_START, TASK_DAY, _session

pytestmark = pytest.mark.no_postgres


@pytest.fixture(autouse=True)
def current_period(monkeypatch):
    monkeypatch.setattr(recovery, "_now", lambda: DAY_START + timedelta(hours=1))


def _reserve(session, task_id, *, units, identity="source"):
    ledger = session.scalar(select(TaskDayLedger).where(TaskDayLedger.task_id == task_id))
    return reserve_portfolio_units(session, session.get(Task, task_id), ledger,
        action_class="reaction", demand_identity=identity, requested_units_by_account={11: units})


def _release(session, task_id):
    for row in session.scalars(select(AccountPortfolioLoadReservation).where(
            AccountPortfolioLoadReservation.task_id == task_id)):
        row.state = "released"
    session.commit()


@pytest.mark.parametrize("occupied", [1, 2])
def test_released_capacity_adds_only_original_deficit(occupied):
    with _session() as session:
        _reserve(session, "task-a", units=occupied)
        original = _reserve(session, "task-b", units=2)
        original_snapshot = list(original.plan.account_task_day_load)
        _release(session, "task-a")
        restored = _reserve(session, "task-b", units=2)
        session.commit()
        replay = _reserve(session, "task-b", units=2)
        assert restored.allocated_units == 2 and restored.deficit_units == 0
        assert restored.plan.id != original.plan.id and original.plan.state == "superseded"
        assert original.plan.account_task_day_load == original_snapshot
        assert replay.plan.id == restored.plan.id
        assert session.scalar(select(func.count(PortfolioFeasibilityPlanRevision.id))) == 3
        rows = session.scalars(select(AccountPortfolioLoadReservation).where(
            AccountPortfolioLoadReservation.task_id == "task-b")).all()
        assert len(rows) == 1 and rows[0].reserved_units == 2


def test_qualification_recovery_revisits_original_zero_plan():
    with _session() as session:
        account = session.get(TgAccount, 11)
        account.telegram_frozen = True
        session.commit()
        original = _reserve(session, "task-b", units=1)
        session.commit()
        account.telegram_frozen = False
        session.commit()
        restored = _reserve(session, "task-b", units=1)
        assert original.allocated_units == 0 and restored.allocated_units == 1
        assert restored.plan.id != original.plan.id


def test_released_original_assignment_is_not_resurrected():
    with _session() as session:
        _reserve(session, "task-a", units=1)
        original = _reserve(session, "task-b", units=2)
        _release(session, "task-a")
        _release(session, "task-b")
        replay = _reserve(session, "task-b", units=2)
        assert replay.plan.id == original.plan.id and replay.allocated_units == 0
        row = session.scalar(select(AccountPortfolioLoadReservation).where(
            AccountPortfolioLoadReservation.task_id == "task-b"))
        assert row.state == "released" and row.reserved_units == 1


@pytest.mark.parametrize("closed", ["deadline", "paused", "ledger"])
def test_closed_business_scope_cannot_gain_new_allocation(monkeypatch, closed):
    with _session() as session:
        _reserve(session, "task-a", units=2)
        original = _reserve(session, "task-b", units=1)
        _release(session, "task-a")
        if closed == "deadline":
            monkeypatch.setattr(recovery, "_now", lambda: DAY_START + timedelta(days=1))
        elif closed == "paused":
            session.get(Task, "task-b").status = "paused"
        else:
            session.get(TaskDayLedger, "ledger-b").lifecycle_status = "closed"
        session.commit()
        replay = _reserve(session, "task-b", units=1)
        assert replay.plan.id == original.plan.id and replay.allocated_units == 0


@pytest.mark.parametrize("outcome", ["confirmed", "unknown", "call_issued"])
def test_recovery_keeps_remote_budget_occupancy(outcome):
    with _session() as session:
        _reserve(session, "task-a", units=2)
        original = _reserve(session, "task-b", units=1)
        _release(session, "task-a")
        policy = session.scalar(select(AccountBehaviorBudgetPolicyRevision))
        ledger = AccountBehaviorBudgetLedger(tenant_id=1, account_id=11, task_day=TASK_DAY,
            policy_revision_id=policy.id, action_budgets=policy.action_budgets,
            counters={"reaction": {outcome: 2}})
        session.add(ledger)
        session.commit()
        replay = _reserve(session, "task-b", units=1)
        assert replay.plan.id == original.plan.id and replay.allocated_units == 0
        assert ledger.counters == {"reaction": {outcome: 2}}


def test_changed_zero_demand_cannot_bypass_frozen_identity():
    with _session() as session:
        _reserve(session, "task-a", units=2)
        original = _reserve(session, "task-b", units=1)
        _release(session, "task-a")
        changed = _reserve(session, "task-b", units=2)
        assert changed.plan.id == original.plan.id and changed.allocated_units == 0
        assert session.get(Task, "task-b").last_error == "portfolio_input_changed_after_freeze"
