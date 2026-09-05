from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from queue import Queue
from time import monotonic, sleep

import pytest
from sqlalchemy import delete, event, text

from app.database import SessionLocal
from app.models import (
    AccountBehaviorBudgetLedger, AccountBehaviorBudgetPolicyRevision, Task, TaskDayLedger, Tenant, TgAccount,
)
from app.services.task_center.engagement_portfolio import _allocate_request, reserve_portfolio_units


pytestmark = pytest.mark.allow_missing_rule_binding
TENANT_ID = 954_510
FIRST_ACCOUNT_ID = 954_520
CANDIDATE_COUNT = 32
MAX_CAPACITY_READ_QUERIES = 4
LOCK_WAIT_SECONDS = 3
LOCK_POLL_SECONDS = .01
RESULT_WAIT_SECONDS = 6
PERIOD_START = datetime(2026, 9, 5, 16, tzinfo=timezone.utc)


def _seed(session):
    session.add(Tenant(id=TENANT_ID, name="组合容量批量读取"))
    session.flush()
    policy = AccountBehaviorBudgetPolicyRevision(tenant_id=TENANT_ID, account_class="normal",
        action_budgets={"total": 4, "reaction": 2, "view": 3})
    session.add(policy)
    account_ids = list(range(FIRST_ACCOUNT_ID, FIRST_ACCOUNT_ID + CANDIDATE_COUNT))
    session.add_all([TgAccount(id=key, tenant_id=TENANT_ID, display_name="容量测试",
        phone_masked="test") for key in account_ids])
    tasks = [Task(tenant_id=TENANT_ID, name=name, type="channel_like", status="running",
        type_config={"engagement_contract_version": "unified_engagement_v1"}) for name in ("先规划", "后规划")]
    session.add_all(tasks)
    session.flush()
    ledgers = [TaskDayLedger(tenant_id=TENANT_ID, task_id=task.id, timezone_snapshot="Asia/Shanghai",
        timezone_revision=1, obligation_local_date=(PERIOD_START + timedelta(hours=8)).date(),
        period_start_at=PERIOD_START, deadline_at=PERIOD_START + timedelta(days=1),
        day_phase="full_day", planning_anchor_at=PERIOD_START) for task in tasks]
    session.add_all(ledgers)
    session.flush()
    return tasks, ledgers, account_ids, policy


def test_postgres_batch_reads_classes_and_day_in_four_queries():
    with SessionLocal() as session:
        tasks, ledgers, account_ids, policy = _seed(session)
        session.add(AccountBehaviorBudgetLedger(tenant_id=TENANT_ID, account_id=account_ids[0],
            task_day=ledgers[0].obligation_local_date, policy_revision_id=policy.id,
            counters={"view": {"confirmed": 3}}))
        session.flush()
        request = {"candidate_account_ids": account_ids, "requested_units": 2 * CANDIDATE_COUNT,
            "requested_units_by_account": {str(key): 2 for key in account_ids}}
        statements = []
        connection = session.connection()
        def record(_connection, _cursor, statement, *_args):
            statements.append(statement)
        event.listen(connection, "before_cursor_execute", record)
        try:
            allocation, capacities, policy_ids = _allocate_request(session, tasks[0], ledgers[0],
                action_class="reaction", request=request)
        finally:
            event.remove(connection, "before_cursor_execute", record)
        assert len(statements) == MAX_CAPACITY_READ_QUERIES
        assert allocation == capacities == {key: 1 if key == account_ids[0] else 2 for key in account_ids}
        assert policy_ids == [policy.id]
        session.rollback()


def _reserve_other_task(ids, started):
    with SessionLocal() as session:
        task, ledger = session.get(Task, ids[0]), session.get(TaskDayLedger, ids[1])
        started.put(session.scalar(text("select pg_backend_pid()")))
        result = reserve_portfolio_units(session, task, ledger, action_class="reaction",
            demand_identity="second-source", requested_units_by_account={FIRST_ACCOUNT_ID: 1})
        session.commit()
        return result.allocated_units, result.deficit_units


def _wait_for_policy_lock(session, waiter, owner):
    deadline = monotonic() + LOCK_WAIT_SECONDS
    while monotonic() < deadline:
        if owner in session.scalar(text("select pg_blocking_pids(:pid)"), {"pid": waiter}):
            return True
        sleep(LOCK_POLL_SECONDS)
    return False


def test_concurrent_plans_still_serialize_before_reading_reserved_capacity():
    with SessionLocal() as session:
        tasks, ledgers, _, _ = _seed(session)
        ids = ((tasks[0].id, ledgers[0].id), (tasks[1].id, ledgers[1].id))
        session.commit()
    try:
        with SessionLocal() as writer, ThreadPoolExecutor(max_workers=1) as executor:
            first = reserve_portfolio_units(writer, writer.get(Task, ids[0][0]),
                writer.get(TaskDayLedger, ids[0][1]), action_class="reaction",
                demand_identity="first-source", requested_units_by_account={FIRST_ACCOUNT_ID: 2})
            assert first.allocated_units == 2
            owner = writer.scalar(text("select pg_backend_pid()"))
            started = Queue()
            future = executor.submit(_reserve_other_task, ids[1], started)
            try:
                blocked = _wait_for_policy_lock(writer, started.get(timeout=LOCK_WAIT_SECONDS), owner)
            finally:
                writer.commit()
            assert blocked
            assert future.result(timeout=RESULT_WAIT_SECONDS) == (0, 1)
    finally:
        _cleanup()


def _cleanup():
    with SessionLocal() as session:
        for model in (Task, AccountBehaviorBudgetLedger, TgAccount, AccountBehaviorBudgetPolicyRevision):
            session.execute(delete(model).where(model.tenant_id == TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()
