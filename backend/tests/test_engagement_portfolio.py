from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountBehaviorBudgetLedger,
    AccountBehaviorBudgetPolicyRevision,
    AccountPortfolioLoadReservation,
    PortfolioFeasibilityPlanRevision,
    Task,
    TaskDayLedger,
    Tenant,
    TgAccount,
)
from app.services.task_center.engagement_portfolio import (
    reserve_portfolio_units,
    task_account_portfolio_allowance,
)


pytestmark = pytest.mark.no_postgres
TASK_DAY = date(2026, 9, 4)
DAY_START = datetime(2026, 9, 3, 16, tzinfo=timezone.utc)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(
        AccountBehaviorBudgetPolicyRevision(
            tenant_id=1,
            account_class="normal",
            action_budgets={"total": 4, "reaction": 2, "view": 3},
        )
    )
    session.add_all([_account(11), _account(12)])
    session.add_all([_task("task-a"), _task("task-b")])
    session.flush()
    session.add_all([_ledger("ledger-a", "task-a"), _ledger("ledger-b", "task-b")])
    session.commit()
    return session


def _account(account_id: int) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=1,
        display_name=f"账号{account_id}",
        phone_masked=str(account_id),
        status="在线",
    )


def _task(task_id: str) -> Task:
    return Task(
        id=task_id,
        tenant_id=1,
        name=task_id,
        type="channel_like",
        status="running",
        type_config={"engagement_contract_version": "unified_engagement_v1"},
    )


def _ledger(ledger_id: str, task_id: str) -> TaskDayLedger:
    return TaskDayLedger(
        id=ledger_id,
        tenant_id=1,
        task_id=task_id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=TASK_DAY,
        period_start_at=DAY_START,
        deadline_at=DAY_START.replace(day=4),
        day_phase="full_day",
        planning_anchor_at=DAY_START,
    )


def test_existing_task_reservation_cannot_be_stolen_by_later_task() -> None:
    with _session() as session:
        first = reserve_portfolio_units(
            session,
            session.get(Task, "task-a"),
            session.get(TaskDayLedger, "ledger-a"),
            action_class="reaction",
            demand_identity="source:1",
            requested_units_by_account={11: 2},
        )
        second = reserve_portfolio_units(
            session,
            session.get(Task, "task-b"),
            session.get(TaskDayLedger, "ledger-b"),
            action_class="reaction",
            demand_identity="source:2",
            requested_units_by_account={11: 1},
        )
        session.commit()

        assert first.achievable
        assert first.allocated_units_by_account == {11: 2}
        assert not second.achievable
        assert second.allocated_units == 0
        assert second.deficit_units == 1
        assert second.plan.decision == "structurally_unachievable"
        assert task_account_portfolio_allowance(
            session,
            task_id="task-b",
            task_day=TASK_DAY,
            account_id=11,
            action_class="reaction",
        ) == (0, 0)


def test_full_deficit_replay_returns_same_plan_without_duplicate() -> None:
    with _session() as session:
        task_a = session.get(Task, "task-a")
        ledger_a = session.get(TaskDayLedger, "ledger-a")
        reserve_portfolio_units(
            session,
            task_a,
            ledger_a,
            action_class="reaction",
            demand_identity="source:1",
            requested_units_by_account={11: 2},
        )
        task_b = session.get(Task, "task-b")
        ledger_b = session.get(TaskDayLedger, "ledger-b")
        first = reserve_portfolio_units(
            session,
            task_b,
            ledger_b,
            action_class="reaction",
            demand_identity="source:2",
            requested_units_by_account={11: 1},
        )
        replay = reserve_portfolio_units(
            session,
            task_b,
            ledger_b,
            action_class="reaction",
            demand_identity="source:2",
            requested_units_by_account={11: 1},
        )

        assert replay.plan.id == first.plan.id
        assert replay.deficit_units == 1
        assert session.scalar(
            select(func.count(PortfolioFeasibilityPlanRevision.id))
        ) == 2


def test_actual_unplanned_usage_reduces_new_portfolio_capacity() -> None:
    with _session() as session:
        session.add(
            AccountBehaviorBudgetLedger(
                tenant_id=1,
                account_id=11,
                task_day=TASK_DAY,
                policy_revision_id=session.scalar(
                    select(AccountBehaviorBudgetPolicyRevision.id)
                ),
                action_budgets={"reaction": 2},
                counters={"reaction": {"confirmed": 1}},
            )
        )
        session.flush()

        decision = reserve_portfolio_units(
            session,
            session.get(Task, "task-a"),
            session.get(TaskDayLedger, "ledger-a"),
            action_class="reaction",
            demand_identity="source:actual-aware",
            total_units=2,
            candidate_account_ids=[11],
        )

        assert decision.allocated_units_by_account == {11: 1}
        assert decision.deficit_units == 1
        assert session.scalar(
            select(func.sum(AccountPortfolioLoadReservation.reserved_units))
        ) == 1


def test_empty_candidate_pool_is_persisted_as_structural_deficit() -> None:
    with _session() as session:
        decision = reserve_portfolio_units(
            session,
            session.get(Task, "task-a"),
            session.get(TaskDayLedger, "ledger-a"),
            action_class="reaction",
            demand_identity="source:no-eligible-account",
            total_units=2,
            candidate_account_ids=[],
        )

        assert decision.allocated_units_by_account == {}
        assert decision.deficit_units == 2
        assert decision.plan.decision == "structurally_unachievable"
        assert decision.plan.account_task_day_load == []


def test_cross_adapter_total_budget_limits_later_task() -> None:
    with _session() as session:
        task_a = session.get(Task, "task-a")
        ledger_a = session.get(TaskDayLedger, "ledger-a")
        reaction = reserve_portfolio_units(
            session,
            task_a,
            ledger_a,
            action_class="reaction",
            demand_identity="source:reaction",
            requested_units_by_account={11: 2},
        )
        task_b = session.get(Task, "task-b")
        ledger_b = session.get(TaskDayLedger, "ledger-b")
        view = reserve_portfolio_units(
            session,
            task_b,
            ledger_b,
            action_class="view",
            demand_identity="source:view",
            requested_units_by_account={11: 3},
        )

        assert reaction.allocated_units_by_account == {11: 2}
        assert view.allocated_units_by_account == {11: 2}
        assert view.deficit_units == 1


def test_changed_demand_preserves_frozen_overlap_without_unique_key_failure() -> None:
    with _session() as session:
        task = session.get(Task, "task-a")
        ledger = session.get(TaskDayLedger, "ledger-a")
        first = reserve_portfolio_units(
            session,
            task,
            ledger,
            action_class="reaction",
            demand_identity="source:mutable-candidates",
            requested_units_by_account={11: 1},
        )
        changed = reserve_portfolio_units(
            session,
            task,
            ledger,
            action_class="reaction",
            demand_identity="source:mutable-candidates",
            requested_units_by_account={11: 1, 12: 1},
        )

        assert changed.plan.id == first.plan.id
        assert changed.allocated_units_by_account == {11: 1}
        assert changed.deficit_units == 1
        assert task.last_error == "portfolio_input_changed_after_freeze"
        assert session.scalar(
            select(func.count(AccountPortfolioLoadReservation.id))
        ) == 1
