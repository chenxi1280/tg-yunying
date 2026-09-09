"""Late admission extends the original demand once under real row contention."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import AccountPortfolioLoadReservation, PortfolioFeasibilityPlanRevision, Task, TaskDayLedger
from app.services.task_center import engagement_portfolio_recovery as recovery
from app.services.task_center.engagement_portfolio import reserve_portfolio_units
from tests.test_engagement_portfolio import DAY_START
from tests.test_engagement_portfolio_query_postgres import _wait_for_policy_lock
from tests.test_execution_progress_postgres import _seed_portfolio
from tests.test_runtime_retention_protection_postgres import database

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]
WAIT_SECONDS = 6


def _reserve(session, admitted):
    return reserve_portfolio_units(session, session.get(Task, "task-a"),
        session.get(TaskDayLedger, "ledger-a"), action_class="reaction",
        demand_identity="original-admission-demand", total_units=3,
        candidate_account_ids=[11, 12], allocatable_account_ids=frozenset(admitted))


def _competing_joined_plan(database, started):
    with Session(database) as session:
        started.put(session.scalar(text("select pg_backend_pid()")))
        result = _reserve(session, {11, 12})
        session.commit()
        return result.plan.id, result.allocated_units_by_account


def test_concurrent_late_admission_preserves_one_demand_and_one_successor(database, monkeypatch):
    monkeypatch.setattr(recovery, "_now", lambda: DAY_START + timedelta(hours=1))
    with Session(database) as seed:
        _seed_portfolio(seed)
        first = _reserve(seed, {11})
        assert first.allocated_units_by_account == {11: 2}
        frozen_demand = first.plan.demand_snapshot
        seed.commit()
    with Session(database) as writer, ThreadPoolExecutor(max_workers=1) as executor:
        joined = _reserve(writer, {11, 12})
        owner = writer.scalar(text("select pg_backend_pid()"))
        started = Queue()
        future = executor.submit(_competing_joined_plan, database, started)
        try:
            assert _wait_for_policy_lock(writer, started.get(timeout=WAIT_SECONDS), owner)
        finally:
            writer.commit()
        assert future.result(timeout=WAIT_SECONDS) == (joined.plan.id, {11: 2, 12: 1})
    with Session(database) as check:
        plans = list(check.scalars(select(PortfolioFeasibilityPlanRevision)))
        assert len(plans) == 2
        assert all(plan.demand_snapshot == frozen_demand for plan in plans)
        assert check.scalar(select(func.count(AccountPortfolioLoadReservation.id))) == 2
        assert check.scalar(select(func.sum(AccountPortfolioLoadReservation.reserved_units))) == 3
