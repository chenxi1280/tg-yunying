"""Real row-lock contention and deficit recovery, using an isolated test schema."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Queue
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    AccountBehaviorBudgetPolicyRevision, AccountPortfolioLoadReservation, Action, ExecutionAttempt,
    PortfolioFeasibilityPlanRevision, StageWakeOutbox, Task, TaskDayLedger, TaskPlannerWakeState,
    Tenant, TgAccount, TgAccountAuthorization, TgAccountOnlineState,
)
from app.services._common import _now
from app.services.task_center import dispatcher, engagement_membership_wake as wakes
from app.services.task_center import engagement_portfolio_recovery as recovery
from app.services.task_center.account_assignment_eligibility import assignment_decisions
from app.services.task_center.engagement_policy_scope import policy_eligible_member_ids
from app.services.task_center.engagement_portfolio import reserve_portfolio_units
from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
from tests.test_engagement_assignment_postgres import _seed as seed_identity
from tests.test_engagement_membership_foundation import _initialize, _seed as seed_membership, _task as membership_task
from tests.test_engagement_portfolio import DAY_START, _account, _ledger, _task
from tests.test_engagement_portfolio_query_postgres import _wait_for_policy_lock
from tests.test_runtime_retention_protection_postgres import database

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]
WAIT_SECONDS = 6


def _attempt_worker(database, action_id, *, start, finished, release):
    with Session(database) as session:
        action = session.get(Action, action_id)
        account = session.get(TgAccount, 11)
        start.wait(timeout=WAIT_SECONDS)
        try:
            dispatcher._begin_execution_attempt(session, action, account)
            outcome = "created"
        except RuntimeResourceBlocked as error:
            outcome = error.code
        finished.put(outcome)
        assert release.wait(timeout=WAIT_SECONDS)
        session.commit()
        return outcome


def test_actual_attempt_creation_has_no_share_to_update_deadlock(database):
    with Session(database) as seed:
        seed_identity(seed)
        actions = [Action(tenant_id=1, task_id="qa-task", task_type="channel_view",
            action_type="view_message", account_id=11) for _ in range(2)]
        seed.add_all(actions)
        seed.commit()
        ids = [action.id for action in actions]
    start, finished, release = Barrier(2), Queue(), Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_attempt_worker, database, identity,
            start=start, finished=finished, release=release) for identity in ids]
        try:
            results = [finished.get(timeout=WAIT_SECONDS) for _ in ids]
        finally:
            release.set()
        assert sorted(results) == ["account_execution_busy", "created"]
        assert sorted(future.result(timeout=WAIT_SECONDS) for future in futures) == sorted(results)
    with Session(database) as session:
        assert session.scalar(select(func.count(ExecutionAttempt.id))) == 1


@pytest.mark.parametrize("kind", ["account", "authorization", "online"])
def test_busy_qualification_is_local_and_does_not_shrink_participation(database, kind):
    with Session(database) as seed:
        seed_identity(seed)
        seed.add(TgAccountOnlineState(tenant_id=1, account_id=11))
        authorization = TgAccountAuthorization(tenant_id=1, account_id=11, is_current=True,
            status="active", session_ciphertext="QA")
        seed.add(authorization)
        seed.flush()
        seed.get(TgAccount, 11).current_authorization_id = authorization.id
        seed.commit()
    snapshot = SimpleNamespace(member_account_ids=[11, 12],
        group_memberships=[{"member_contracts": [{"account_id": 11}, {"account_id": 12}]}])
    with Session(database) as writer, Session(database) as planner:
        model = {"account": TgAccount, "authorization": TgAccountAuthorization, "online": TgAccountOnlineState}[kind]
        column = model.id if kind == "account" else model.account_id
        writer.scalar(select(model).where(column == 11).with_for_update())
        task = planner.get(Task, "qa-task")
        assert assignment_decisions(planner, 1, [11, 12], skip_busy=True) == {11: "account_eligibility_busy", 12: ""}
        assert policy_eligible_member_ids(planner, task, snapshot) == (11, 12)
        summary = task.stats["account_assignment_eligibility"]
        assert summary["pending_count"] == summary["eligible_count"] == 1
        assert summary["excluded_count"] == 0
        writer.rollback()
        assert assignment_decisions(planner, 1, [11, 12], skip_busy=True) == {11: "", 12: ""}


def _seed_portfolio(session):
    session.add(Tenant(id=1, name="recovery QA"))
    session.flush()
    session.add(AccountBehaviorBudgetPolicyRevision(tenant_id=1, account_class="normal",
        action_budgets={"total": 4, "reaction": 2, "view": 3}))
    session.add_all([_account(11), _account(12), _task("task-a"), _task("task-b")])
    session.flush()
    session.add_all([_ledger("ledger-a", "task-a"), _ledger("ledger-b", "task-b")])
    session.commit()


def _reserve(session, task_id, *, units):
    ledger = session.scalar(select(TaskDayLedger).where(TaskDayLedger.task_id == task_id))
    return reserve_portfolio_units(session, session.get(Task, task_id), ledger,
        action_class="reaction", demand_identity="source", requested_units_by_account={11: units})


def _concurrent_recovery(database, started):
    with Session(database) as session:
        started.put(session.scalar(text("select pg_backend_pid()")))
        result = _reserve(session, "task-b", units=1)
        session.commit()
        return result.plan.id, result.allocated_units


def test_two_recovery_transactions_create_one_successor_and_one_reservation(database, monkeypatch):
    monkeypatch.setattr(recovery, "_now", lambda: DAY_START + timedelta(hours=1))
    with Session(database) as seed:
        _seed_portfolio(seed)
        _reserve(seed, "task-a", units=2)
        assert _reserve(seed, "task-b", units=1).allocated_units == 0
        seed.commit()
        first = seed.scalar(select(AccountPortfolioLoadReservation).where(AccountPortfolioLoadReservation.task_id == "task-a"))
        first.state = "released"
        seed.commit()
    with Session(database) as writer, ThreadPoolExecutor(max_workers=1) as executor:
        restored = _reserve(writer, "task-b", units=1)
        owner = writer.scalar(text("select pg_backend_pid()"))
        started = Queue()
        future = executor.submit(_concurrent_recovery, database, started)
        try:
            assert _wait_for_policy_lock(writer, started.get(timeout=WAIT_SECONDS), owner)
        finally:
            writer.commit()
        assert future.result(timeout=WAIT_SECONDS) == (restored.plan.id, 1)
    with Session(database) as check:
        assert check.scalar(select(func.count(PortfolioFeasibilityPlanRevision.id))) == 3
        rows = check.scalars(select(AccountPortfolioLoadReservation).where(AccountPortfolioLoadReservation.task_id == "task-b")).all()
        assert len(rows) == 1 and rows[0].reserved_units == 1


@pytest.mark.parametrize("database_timezone", ["UTC", "Asia/Shanghai"])
def test_busy_task_cannot_rollback_healthy_membership_wake_delivery(database, monkeypatch, database_timezone):
    def set_timezone(connection, _record):
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('TimeZone', %s, false)", (database_timezone,))
    event.listen(database, "connect", set_timezone)
    database.dispose()
    with Session(database) as seed:
        seed_membership(seed)
        _initialize(seed)
        healthy, busy = membership_task(seed), membership_task(seed)
        seed.commit()
        healthy_id, busy_id = healthy.id, busy.id
    current = _now()
    monkeypatch.setattr(wakes, "_now", lambda: current)
    factory = sessionmaker(bind=database)
    with Session(database) as owner:
        owner.scalar(select(Task).where(Task.id == busy_id).with_for_update())
        wakes.drain_membership_wake_transactions(factory)
        with Session(database) as check:
            assert check.scalar(select(TaskPlannerWakeState.wake_revision).where(TaskPlannerWakeState.task_id == healthy_id)) == 2
            assert check.scalar(select(TaskPlannerWakeState.id).where(TaskPlannerWakeState.task_id == busy_id)) is None
            assert check.scalar(select(func.count(StageWakeOutbox.id)).where(StageWakeOutbox.state == "expanded")) == 2
        owner.rollback()
    monkeypatch.setattr(wakes, "_now", lambda: current + timedelta(seconds=wakes.WAKE_RETRY_SECONDS + 1))
    assert wakes.drain_membership_wake_transactions(factory) == 2
    assert wakes.drain_membership_wake_transactions(factory) == 0
    with Session(database) as check:
        states = dict(check.execute(select(TaskPlannerWakeState.task_id, TaskPlannerWakeState.wake_revision)).all())
        assert states == {healthy_id: 2, busy_id: 2}
