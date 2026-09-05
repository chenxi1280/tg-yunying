from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, select

from app.models import (AccountBehaviorBudgetLedger, AccountBehaviorBudgetPolicyRevision,
    AccountPortfolioLoadReservation, Task, TaskDayLedger, Tenant, TgAccount)
from app.services.task_center.engagement_portfolio import _allocate_request, reserve_portfolio_units
from tests.test_engagement_portfolio import TASK_DAY, _account, _session


pytestmark = pytest.mark.no_postgres
MAX_CAPACITY_READ_QUERIES = 4


def _query_allocation(session, account_ids):
    task, ledger = session.get(Task, "task-a"), session.get(TaskDayLedger, "ledger-a")
    request = {"candidate_account_ids": account_ids, "requested_units": len(account_ids) * 2,
        "requested_units_by_account": {str(key): 2 for key in account_ids}}
    statements = []
    connection = session.connection()
    def record(_connection, _cursor, statement, *_args):
        statements.append(statement)
    event.listen(connection, "before_cursor_execute", record)
    try:
        result = _allocate_request(session, task, ledger, action_class="reaction", request=request)
    finally:
        event.remove(connection, "before_cursor_execute", record)
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    return result, len(statements)


@pytest.mark.parametrize("account_count", [1, 32, 128])
def test_portfolio_capacity_query_count_does_not_grow_with_candidate_accounts(account_count):
    with _session() as session:
        account_ids = list(range(100, 100 + account_count))
        session.add_all([_account(key) for key in account_ids])
        session.flush()
        (allocation, capacities, policy_ids), queries = _query_allocation(session, account_ids)
        assert allocation == capacities == {key: 2 for key in account_ids}
        assert len(policy_ids) == 1
        assert queries <= MAX_CAPACITY_READ_QUERIES, queries


def test_batch_capacity_keeps_day_and_class_totals_separate():
    with _session() as session:
        policy = session.scalar(select(AccountBehaviorBudgetPolicyRevision))
        session.add_all([
            AccountBehaviorBudgetLedger(tenant_id=1, account_id=11, task_day=TASK_DAY,
                policy_revision_id=policy.id, counters={"view": {"confirmed": 3}}),
            AccountBehaviorBudgetLedger(tenant_id=1, account_id=12, task_day=TASK_DAY - timedelta(days=1),
                policy_revision_id=policy.id, counters={"reaction": {"unknown": 2}}),
        ])
        session.flush()
        (allocation, capacities, _), _ = _query_allocation(session, [11, 12])
        assert capacities == allocation == {11: 1, 12: 2}


def test_absent_and_deleted_candidates_keep_zero_capacity():
    with _session() as session:
        account = session.get(TgAccount, 12)
        account.deleted_at = datetime.now()
        session.flush()
        (allocation, capacities, _), _ = _query_allocation(session, [11, 12, 99])
        assert allocation == {11: 2}
        assert capacities == {11: 2, 12: 0, 99: 0}


@pytest.mark.parametrize("changed", ["ledger", "policy", "account_class"])
def test_autoflush_disabled_keeps_existing_session_updates_visible(changed):
    with _session() as session:
        policy = session.scalar(select(AccountBehaviorBudgetPolicyRevision))
        ledger = AccountBehaviorBudgetLedger(tenant_id=1, account_id=11, task_day=TASK_DAY,
            policy_revision_id=policy.id, counters={})
        session.add(ledger)
        session.add(AccountBehaviorBudgetPolicyRevision(tenant_id=1, account_class="secondary",
            action_budgets={"total": 1, "reaction": 2, "view": 3}))
        session.flush()
        account = session.get(TgAccount, 11)
        session.autoflush = False
        if changed == "ledger":
            ledger.counters = {"view": {"unowned": 3}}
        if changed == "policy":
            policy.action_budgets = {"total": 1, "reaction": 2, "view": 3}
        if changed == "account_class":
            account.account_identity = "secondary"
        (allocation, capacities, _), queries = _query_allocation(session, [11])
        assert allocation == capacities == {11: 1}
        assert queries <= MAX_CAPACITY_READ_QUERIES


def test_released_plans_do_not_consume_capacity_and_other_tenant_has_no_candidates():
    with _session() as session:
        task, ledger = session.get(Task, "task-a"), session.get(TaskDayLedger, "ledger-a")
        reserve_portfolio_units(session, task, ledger, action_class="view",
            demand_identity="expired-source", requested_units_by_account={11: 3})
        session.flush()
        session.scalar(select(AccountPortfolioLoadReservation)).state = "released"
        session.add(Tenant(id=2, name="另一租户"))
        session.add(TgAccount(id=99, tenant_id=2, display_name="不属于本任务", phone_masked="test"))
        session.flush()
        (allocation, capacities, _), queries = _query_allocation(session, [11, 99])
        assert allocation == {11: 2} and capacities == {11: 2, 99: 0}
        assert queries <= MAX_CAPACITY_READ_QUERIES


def test_missing_class_policy_keeps_explicit_failure():
    with _session() as session:
        session.get(TgAccount, 11).account_identity = "secondary"
        session.flush()
        with pytest.raises(RuntimeError, match="account_behavior_budget_policy_missing"):
            _query_allocation(session, [11])
