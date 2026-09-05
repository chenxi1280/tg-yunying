from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from queue import Queue
from time import monotonic, sleep

import pytest
from sqlalchemy import delete, select, text

from app.database import SessionLocal
from app.models import (
    AccountBehaviorBudgetLedger, AccountBehaviorBudgetPolicyRevision, AccountBehaviorBudgetReservation,
    AccountPool, Action, ExecutionAttempt, RemoteInvocationFence, Task, Tenant, TgAccount,
)
from app.services._common import _now
from app.services.task_center.engagement_binding import (
    freeze_initial_binding, freeze_membership_snapshot, validate_engagement_binding,
)
from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
from app.services.task_center.engagement_runtime_resources import (
    mark_attempt_call_issued, reserve_attempt_resources, settle_attempt_resources,
)
from app.services.task_center.engagement_shared_usage import SharedUsageScope, read_shared_account_usage
from app.services.task_center.engagement_unowned_activity import _charge_behavior_budget
from app.timezone import as_beijing


pytestmark = pytest.mark.allow_missing_rule_binding
TENANT_ID, POOL_ID, ACCOUNT_ID = 954_401, 954_402, 954_403
LOCK_OBSERVATION_SECONDS = 3
LOCK_POLL_SECONDS = 0.01
THREAD_RESULT_SECONDS = 6
BEIJING_MIDNIGHT = datetime(2026, 9, 4, 16, tzinfo=timezone.utc)


def _seed(session):
    session.add(Tenant(id=TENANT_ID, name="共享账号用量测试"))
    session.flush()
    session.add(AccountPool(id=POOL_ID, tenant_id=TENANT_ID, name="原组"))
    session.flush()
    session.add(TgAccount(id=ACCOUNT_ID, tenant_id=TENANT_ID, pool_id=POOL_ID,
        display_name="原账号", phone_masked="test"))
    session.add(AccountBehaviorBudgetPolicyRevision(tenant_id=TENANT_ID, account_class="normal",
        action_budgets={"total": 1, "reaction": 10, "view": 10}))
    config = {"engagement_contract_version": "unified_engagement_v1",
        "account_selection_mode": "group", "account_group_ids": [POOL_ID]}
    task = Task(tenant_id=TENANT_ID, type="channel_like", name="旧日新调用", type_config=config)
    session.add(task)
    session.flush()
    binding = freeze_initial_binding(session, task,
        validate_engagement_binding(session, TENANT_ID, task.type, config))
    session.flush()
    snapshot = freeze_membership_snapshot(session, task,
        participation_unit=f"legacy_cutover:{binding.id}")
    original = as_beijing(_now()) - timedelta(days=1)
    action = Action(tenant_id=TENANT_ID, task_id=task.id, task_type=task.type,
        action_type="like_message", account_id=ACCOUNT_ID,
        created_at=original, pacing_due_at=original)
    session.add(action)
    session.flush()
    attempt = ExecutionAttempt(tenant_id=TENANT_ID, action_id=action.id,
        account_id=ACCOUNT_ID, status="before_call")
    session.add(attempt)
    session.flush()
    assert snapshot.member_account_ids == [ACCOUNT_ID]
    return action, attempt


@pytest.mark.parametrize("offset,expected", [(-1, 0), (0, 1), (86400, 0)])
def test_shared_read_keeps_real_utc_call_and_original_beijing_day_separate(offset, expected):
    with SessionLocal() as session:
        session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        action, attempt = _seed(session)
        action.pacing_due_at = BEIJING_MIDNIGHT - timedelta(seconds=1)
        attempt.status = "result_unknown"
        attempt.gateway_call_started_at = BEIJING_MIDNIGHT + timedelta(seconds=offset)
        attempt.after_call_at = attempt.gateway_call_started_at + timedelta(seconds=1)
        attempt.result_snapshot = {"transport_termination_state": "acknowledged"}
        session.flush()
        usage = read_shared_account_usage(session, SharedUsageScope(
            TENANT_ID, ACCOUNT_ID, as_beijing(action.pacing_due_at).date(),
            as_beijing(BEIJING_MIDNIGHT).date()))
        assert dict(usage.original_extra) == {"reaction": 1}
        assert dict(usage.activity_occupied).get("reaction", 0) == expected
        assert usage.legacy_inflight == usage.issues == ()
        assert attempt.status == "result_unknown"
        session.rollback()


def _reserve_in_other_session(ids, started):
    with SessionLocal() as session:
        action = session.get(Action, ids[0])
        attempt = session.get(ExecutionAttempt, ids[1])
        started.put(session.scalar(text("select pg_backend_pid()")))
        try:
            reserve_attempt_resources(session, action, attempt)
            session.commit()
            return "admitted"
        except RuntimeResourceBlocked as exc:
            session.rollback()
            return exc.code


def test_failed_business_result_retains_confirmed_cost_on_original_ledger():
    with SessionLocal() as session:
        action, attempt = _seed(session)
        reserve_attempt_resources(session, action, attempt)
        mark_attempt_call_issued(session, attempt)
        action.status = attempt.status = "failed"
        for _ in range(2):
            settle_attempt_resources(attempt, action, remote_mutation_started=True)
        session.flush()
        session.expire_all()
        reservation = session.scalar(select(AccountBehaviorBudgetReservation).where(
            AccountBehaviorBudgetReservation.attempt_id == attempt.id))
        ledger = session.get(AccountBehaviorBudgetLedger, reservation.ledger_id)
        fence = session.scalar(select(RemoteInvocationFence).where(
            RemoteInvocationFence.attempt_id == attempt.id))
        assert reservation.state == "confirmed" and ledger.counters["reaction"]["confirmed"] == 1
        assert ledger.task_day == as_beijing(action.pacing_due_at).date()
        assert ledger.task_day != as_beijing(attempt.gateway_call_started_at).date()
        assert attempt.status == "failed" and fence.business_outcome_state == "failed"
        session.rollback()


def _wait_for_account_lock(session, waiter_pid, owner_pid):
    deadline = monotonic() + LOCK_OBSERVATION_SECONDS
    while monotonic() < deadline:
        owners = session.scalar(text("select pg_blocking_pids(:pid)"), {"pid": waiter_pid})
        if owner_pid in owners:
            return True
        sleep(LOCK_POLL_SECONDS)
    return False


def test_concurrent_unowned_charge_and_old_day_reservation_share_account_lock():
    with SessionLocal() as session:
        action, attempt = _seed(session)
        ids = (action.id, attempt.id)
        session.commit()
    try:
        with SessionLocal() as writer, ThreadPoolExecutor(max_workers=1) as executor:
            owner_pid = writer.scalar(text("select pg_backend_pid()"))
            _charge_behavior_budget(writer, account_id=ACCOUNT_ID,
                observed_at=_now(), action_class="view")
            writer.flush()
            started = Queue()
            future = executor.submit(_reserve_in_other_session, ids, started)
            try:
                pid = started.get(timeout=LOCK_OBSERVATION_SECONDS)
                blocked = _wait_for_account_lock(writer, pid, owner_pid)
            finally:
                writer.commit()
            assert blocked
            assert future.result(timeout=THREAD_RESULT_SECONDS) == "account_behavior_total_budget_exhausted"
        with SessionLocal() as session:
            ledger = session.scalar(select(AccountBehaviorBudgetLedger).where(
                AccountBehaviorBudgetLedger.account_id == ACCOUNT_ID))
            assert ledger.counters == {"view": {"unowned": 1}}
            assert session.scalar(select(AccountBehaviorBudgetReservation).where(
                AccountBehaviorBudgetReservation.attempt_id == ids[1])) is None
    finally:
        _cleanup_seed()


def _cleanup_seed():
    with SessionLocal() as session:
        for model in (Task, AccountBehaviorBudgetLedger, TgAccount, AccountPool,
                AccountBehaviorBudgetPolicyRevision):
            session.execute(delete(model).where(model.tenant_id == TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()
