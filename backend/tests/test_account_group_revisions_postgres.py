from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Barrier
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import OperationalError

from app.database import SessionLocal
from app.models import (
    AccountGroupMembershipRevision, AccountGroupStateRevision, AccountPool, AuditLog, StageWakeOutbox,
    Task, TaskAccountGroupBindingSetRevision, TaskPlannerWakeState, Tenant, TgAccount,
)
from app.services.account_group_revisions import (
    begin_membership_change, finish_membership_change, initialize_group_revisions,
)
from app.services.account_group_revision_snapshot import current_group_revisions
from app.services.account_group_revision_bootstrap import apply_group_revision_bootstrap, preview_group_revisions
from app.services.account_usage_policy import sync_account_usage
from app.services.task_center.engagement_binding import (
    freeze_initial_binding, freeze_membership_snapshot, validate_engagement_binding,
)
from app.services.task_center.engagement_membership_wake import drain_membership_wake_transactions


pytestmark = pytest.mark.allow_missing_rule_binding
TENANT_ID, SOURCE_POOL, TARGET_POOL, ACCOUNT_ID = 954_610, 954_611, 954_612, 954_613
LOCK_WAIT_SECONDS, RESULT_WAIT_SECONDS, LOCK_POLL_SECONDS = 3, 6, .01


@pytest.fixture
def seeded():
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="成员版本并发"))
        session.flush()
        session.add_all([AccountPool(id=key, tenant_id=TENANT_ID, name=str(key))
            for key in (SOURCE_POOL, TARGET_POOL)])
        session.flush()
        session.add(TgAccount(id=ACCOUNT_ID, tenant_id=TENANT_ID, pool_id=SOURCE_POOL,
            display_name="测试成员", phone_masked="test"))
        session.flush()
        pairs = initialize_group_revisions(session, TENANT_ID, (SOURCE_POOL, TARGET_POOL),
            actor="test", reason="explicit_bootstrap")
        task = Task(tenant_id=TENANT_ID, name="版本消费者", type="channel_view", status="running",
            type_config={"engagement_contract_version": "unified_engagement_v1"})
        session.add(task)
        session.flush()
        session.add(TaskAccountGroupBindingSetRevision(tenant_id=TENANT_ID, task_id=task.id,
            account_group_ids=[SOURCE_POOL], binding_set_hash="test", group_contracts=[]))
        wake_id = session.scalar(select(StageWakeOutbox.id).where(
            StageWakeOutbox.aggregate_id == pairs[0].membership.id))
        task_id = task.id
        session.commit()
    try:
        yield task_id, wake_id
    finally:
        with SessionLocal() as session:
            for model in (Task, StageWakeOutbox, AccountGroupMembershipRevision,
                    AccountGroupStateRevision, TgAccount, AccountPool, AuditLog):
                session.execute(delete(model).where(model.tenant_id == TENANT_ID))
            session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
            session.commit()


def _disable(session, pool_id, *, expected=None):
    token = begin_membership_change(session, TENANT_ID, (pool_id,), actor="test",
        reason="disable", expected_versions=expected)
    session.get(AccountPool, pool_id).is_enabled = False
    return finish_membership_change(session, token)


def _concurrent_disable(started):
    with SessionLocal() as session:
        started.put(session.scalar(text("SELECT pg_backend_pid()")))
        try:
            _disable(session, TARGET_POOL, expected={TARGET_POOL: (1, 1)})
            session.commit()
            return "applied"
        except ValueError as exc:
            session.rollback()
            return str(exc)


def _wait_for_lock(session, waiter, owner):
    deadline = monotonic() + LOCK_WAIT_SECONDS
    while monotonic() < deadline:
        if owner in session.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter}):
            return True
        sleep(LOCK_POLL_SECONDS)
    return False


def test_same_expected_revision_has_one_winner_after_real_tenant_lock_wait(seeded):
    with SessionLocal() as writer, ThreadPoolExecutor(max_workers=1) as executor:
        _disable(writer, TARGET_POOL, expected={TARGET_POOL: (1, 1)})
        owner = writer.scalar(text("SELECT pg_backend_pid()"))
        started = Queue()
        future = executor.submit(_concurrent_disable, started)
        try:
            blocked = _wait_for_lock(writer, started.get(timeout=LOCK_WAIT_SECONDS), owner)
        finally:
            writer.commit()
        assert blocked
        assert future.result(timeout=RESULT_WAIT_SECONDS) == "account_group_revision_conflict"
    with SessionLocal() as session:
        pair, = current_group_revisions(session, TENANT_ID, (TARGET_POOL,))
        assert pair.versions == (1, 2)


def test_stale_identity_map_cannot_admit_move_into_newly_disabled_pool(seeded):
    with SessionLocal() as stale:
        account = stale.get(TgAccount, ACCOUNT_ID)
        target = stale.get(AccountPool, TARGET_POOL)
        assert target.is_enabled
        with SessionLocal() as writer:
            _disable(writer, TARGET_POOL)
            writer.commit()
        with pytest.raises(ValueError, match="account pool disabled"):
            sync_account_usage(stale, account, target, "test")
        stale.rollback()
        assert stale.get(TgAccount, ACCOUNT_ID).pool_id == SOURCE_POOL


@pytest.mark.parametrize("entry", ["account_create", "batch_login"])
def test_both_creation_entries_revalidate_cached_pool_after_tenant_lock(seeded, entry):
    from app.services.accounts import _account_creation_pool
    from app.services.account_login.binding import _create_account
    from app.services.account_login.contracts import BatchLoginError

    with SessionLocal() as stale:
        pool = stale.get(AccountPool, TARGET_POOL)
        assert pool.is_enabled
        with SessionLocal() as writer:
            _disable(writer, TARGET_POOL)
            writer.commit()
        if entry == "account_create":
            with pytest.raises(ValueError, match="account pool disabled"):
                _account_creation_pool(stale, TENANT_ID, TARGET_POOL)
        else:
            item = SimpleNamespace(id=1, tenant_id=TENANT_ID, line_no=1, phone_masked="test")
            with pytest.raises(BatchLoginError, match="目标分组不可用"):
                _create_account(stale, item, TARGET_POOL, "+12025550123", "test")
        stale.rollback()


def _concurrent_batch_binding(started):
    from app.security import encrypt_secret
    from app.services.account_login.binding import bind_or_create_account
    from app.services.account_login.contracts import BatchLoginError

    item = SimpleNamespace(id=1, tenant_id=TENANT_ID, line_no=1, phone_masked="test",
        phone_ciphertext=encrypt_secret("+12025550123"), account_id=None)
    with SessionLocal() as session:
        started.put(session.scalar(text("SELECT pg_backend_pid()")))
        try:
            bind_or_create_account(session, item, TARGET_POOL, "test")
        except BatchLoginError as exc:
            session.rollback()
            return exc.code


def test_batch_login_waits_for_tenant_before_acquiring_phone_advisory_lock(seeded):
    with SessionLocal() as writer, ThreadPoolExecutor(max_workers=1) as executor:
        _disable(writer, TARGET_POOL)
        owner = writer.scalar(text("SELECT pg_backend_pid()"))
        started = Queue()
        future = executor.submit(_concurrent_batch_binding, started)
        try:
            waiter = started.get(timeout=LOCK_WAIT_SECONDS)
            blocked = _wait_for_lock(writer, waiter, owner)
            phone_locks = writer.scalar(text("SELECT count(*) FROM pg_locks "
                "WHERE pid=:pid AND locktype='advisory' AND granted"), {"pid": waiter})
        finally:
            writer.commit()
        assert future.result(timeout=RESULT_WAIT_SECONDS) == "pool_admission_rejected"
        assert blocked and phone_locks == 0


def test_stale_account_move_uses_current_original_group_and_preserves_both_successors(seeded):
    with SessionLocal() as stale:
        account = stale.get(TgAccount, ACCOUNT_ID)
        source = stale.get(AccountPool, SOURCE_POOL)
        with SessionLocal() as writer:
            sync_account_usage(writer, writer.get(TgAccount, ACCOUNT_ID),
                writer.get(AccountPool, TARGET_POOL), "test")
            writer.commit()
        result = sync_account_usage(stale, account, source, "test")
        assert result.previous_pool_id == TARGET_POOL
        stale.commit()
        pairs = current_group_revisions(stale, TENANT_ID, (SOURCE_POOL, TARGET_POOL))
        assert [pair.versions for pair in pairs] == [(3, 1), (3, 1)]
        assert pairs[0].membership.member_account_ids == [ACCOUNT_ID]
        assert pairs[1].membership.member_account_ids == []


def _concurrent_snapshot(task_id, started):
    with SessionLocal() as session:
        started.put(session.scalar(text("SELECT pg_backend_pid()")))
        snapshot = freeze_membership_snapshot(session, session.get(Task, task_id), participation_unit="new-day")
        result = snapshot.member_account_ids, snapshot.group_memberships[0]["membership_revision"]
        session.commit()
        return result


def test_snapshot_waits_for_original_membership_transaction_before_freezing(seeded):
    with SessionLocal() as writer, ThreadPoolExecutor(max_workers=1) as executor:
        sync_account_usage(writer, writer.get(TgAccount, ACCOUNT_ID),
            writer.get(AccountPool, TARGET_POOL), "test")
        owner = writer.scalar(text("SELECT pg_backend_pid()"))
        started = Queue()
        future = executor.submit(_concurrent_snapshot, seeded[0], started)
        try:
            blocked = _wait_for_lock(writer, started.get(timeout=LOCK_WAIT_SECONDS), owner)
        finally:
            writer.commit()
        assert blocked
        assert future.result(timeout=RESULT_WAIT_SECONDS) == ([], 2)


def test_task_lock_contention_keeps_wake_pending_until_atomic_delivery(seeded):
    task_id, wake_id = seeded
    with SessionLocal() as session:
        session.execute(delete(StageWakeOutbox).where(StageWakeOutbox.id != wake_id,
            StageWakeOutbox.tenant_id == TENANT_ID))
        session.commit()
    with SessionLocal() as owner:
        owner.scalar(select(Task).where(Task.id == task_id).with_for_update())
        assert drain_membership_wake_transactions(SessionLocal) == 0
        owner.rollback()
    with SessionLocal() as session:
        assert session.get(StageWakeOutbox, wake_id).state == "pending"
        assert session.scalar(select(TaskPlannerWakeState).where(TaskPlannerWakeState.task_id == task_id)) is None
    assert drain_membership_wake_transactions(SessionLocal) == 1
    assert drain_membership_wake_transactions(SessionLocal) == 0
    with SessionLocal() as session:
        assert session.get(StageWakeOutbox, wake_id).state == "delivered"
        assert session.scalar(select(TaskPlannerWakeState.wake_revision).where(
            TaskPlannerWakeState.task_id == task_id)) == 1


def test_preview_runs_in_postgres_read_only_transaction(seeded):
    with SessionLocal() as session:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        report = preview_group_revisions(session, TENANT_ID)
        assert [row["membership_revision"] for row in report["state"]["groups"]] == [1, 1]
        assert all(row["issue"] is None for row in report["state"]["groups"])


def _concurrent_bootstrap(preview, started):
    with SessionLocal() as session:
        started.put(session.scalar(text("SELECT pg_backend_pid()")))
        try:
            apply_group_revision_bootstrap(session, preview, actor="test", audit_reference="second")
            session.commit()
            return "applied"
        except ValueError as exc:
            session.rollback()
            return str(exc)


def test_concurrent_initial_bootstrap_rejects_second_copy_of_reviewed_snapshot(seeded):
    with SessionLocal() as session:
        for model in (StageWakeOutbox, AccountGroupMembershipRevision, AccountGroupStateRevision):
            session.execute(delete(model).where(model.tenant_id == TENANT_ID))
        session.commit()
        preview = preview_group_revisions(session, TENANT_ID)
    with SessionLocal() as writer, ThreadPoolExecutor(max_workers=1) as executor:
        applied = apply_group_revision_bootstrap(writer, preview, actor="test", audit_reference="first")
        assert applied["initialized_group_count"] == 2
        started = Queue()
        owner = writer.scalar(text("SELECT pg_backend_pid()"))
        future = executor.submit(_concurrent_bootstrap, preview, started)
        try:
            blocked = _wait_for_lock(writer, started.get(timeout=LOCK_WAIT_SECONDS), owner)
        finally:
            writer.commit()
        assert blocked
        assert future.result(timeout=RESULT_WAIT_SECONDS) == "account_group_bootstrap_preview_conflict"


def _concurrent_initial_binding(barrier):
    with SessionLocal() as session:
        config = {"engagement_contract_version": "unified_engagement_v1",
            "account_selection_mode": "group", "account_group_ids": [SOURCE_POOL]}
        task = Task(tenant_id=TENANT_ID, name="并发初始绑定", type="channel_view", type_config=config)
        session.add(task)
        session.flush()
        barrier.wait(timeout=LOCK_WAIT_SECONDS)
        try:
            spec = validate_engagement_binding(session, TENANT_ID, task.type, config)
            freeze_initial_binding(session, task, spec)
            session.commit()
            return "committed"
        except OperationalError as exc:
            session.rollback()
            return exc.orig.sqlstate


def test_two_new_task_foreign_keys_do_not_deadlock_tenant_serialization(seeded):
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_concurrent_initial_binding, barrier) for _ in range(2)]
        assert [future.result(timeout=RESULT_WAIT_SECONDS) for future in futures] == ["committed", "committed"]
