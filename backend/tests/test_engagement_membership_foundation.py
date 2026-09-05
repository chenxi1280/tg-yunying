import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from app.models import (
    AccountGroupMembershipRevision, AccountGroupStateRevision, AccountPool, AccountPoolConcurrencyLease,
    AccountPoolConcurrencyPolicyRevision, Action,
    ExecutionAttempt, StageWakeOutbox, Task, TaskAccountGroupBindingSetRevision,
    TaskPlannerWakeState, TgAccount,
)
from app.services._common import _now
from app.services.account_group_revisions import begin_membership_change, finish_membership_change
from app.services.account_pool_deletion import assert_pool_can_be_deleted
from app.services.task_center.engagement_binding import (
    freeze_initial_binding, freeze_membership_snapshot, validate_engagement_binding,
)
from app.services.task_center import engagement_membership_wake as wakes
from app.services.task_center.engagement_policy_scope import policy_eligible_member_ids
from tests.test_account_group_revisions import _initialize, _seed
from tests.test_engagement_runtime_resources import _session


pytestmark = pytest.mark.no_postgres


def _task(session, *, pool_id=1, status="running", task_type="channel_like"):
    config = {"engagement_contract_version": "unified_engagement_v1",
        "account_selection_mode": "group", "account_group_ids": [pool_id]}
    task = Task(tenant_id=1, name="成员版本消费", type=task_type, status=status, type_config=config)
    session.add(task)
    session.flush()
    freeze_initial_binding(session, task, validate_engagement_binding(session, 1, task.type, config))
    session.flush()
    return task


def test_freeze_requires_existing_revisions_and_does_not_bootstrap():
    with _session() as session:
        _seed(session)
        _initialize(session)
        task = _task(session)
        session.execute(delete(AccountGroupMembershipRevision))
        session.execute(delete(AccountGroupStateRevision))
        with pytest.raises(ValueError, match="account_group_revision_missing"):
            freeze_membership_snapshot(session, task, participation_unit="first")
        assert session.scalar(select(func.count(AccountGroupMembershipRevision.id))) == 0
        assert session.scalar(select(func.count(AccountGroupStateRevision.id))) == 0


def test_freeze_records_membership_and_state_proof_without_rewriting_previous_unit():
    with _session() as session:
        _seed(session)
        original = _initialize(session)[0]
        task = _task(session)
        first = freeze_membership_snapshot(session, task, participation_unit="first")
        session.flush()
        group = first.group_memberships[0]
        assert group["membership_revision_id"] == original.membership.id
        assert group["group_state_revision_id"] == original.state.id
        assert group["member_set_hash"] == original.membership.member_set_hash
        change = begin_membership_change(session, 1, (1,), actor="test", reason="disable")
        session.get(AccountPool, 1).is_enabled = False
        finish_membership_change(session, change)
        second = freeze_membership_snapshot(session, task, participation_unit="second")
        assert first.member_account_ids == second.member_account_ids == [11, 12]
        assert first.group_memberships[0]["group_state"]["is_enabled"] is True
        assert second.group_memberships[0]["group_state"]["is_enabled"] is False
        assert second.group_memberships[0]["group_state_revision"] == 2
        assert policy_eligible_member_ids(session, task, second) == (11, 12)
        assert freeze_membership_snapshot(session, task, participation_unit="first") is first


def test_new_unit_exposes_membership_drift_while_old_snapshot_remains_frozen():
    with _session() as session:
        _seed(session)
        _initialize(session)
        task = _task(session)
        first = freeze_membership_snapshot(session, task, participation_unit="first")
        session.flush()
        session.get(TgAccount, 11).pool_id = 2
        session.flush()
        assert freeze_membership_snapshot(session, task, participation_unit="first") is first
        with pytest.raises(ValueError, match="account_group_revision_drift"):
            freeze_membership_snapshot(session, task, participation_unit="second")
        assert first.member_account_ids == [11, 12]


def test_eligibility_uses_frozen_enabled_state_and_keeps_transient_offline_members():
    with _session() as session:
        _seed(session)
        _initialize(session)
        change = begin_membership_change(session, 1, (1,), actor="test", reason="disable_account")
        session.get(TgAccount, 11).status = "禁用"
        session.get(TgAccount, 12).status = "离线"
        finish_membership_change(session, change)
        task = _task(session)
        snapshot = freeze_membership_snapshot(session, task, participation_unit="first")
        session.get(TgAccount, 11).status = "在线"
        assert snapshot.member_account_ids == [11, 12]
        assert policy_eligible_member_ids(session, task, snapshot) == (12,)


def test_wrong_member_purpose_is_not_silently_removed_from_denominator():
    with _session() as session:
        _seed(session)
        _initialize(session)
        task = _task(session)
        change = begin_membership_change(session, 1, (1,), actor="test", reason="purpose_changed")
        session.get(TgAccount, 11).account_identity = "rank_deboost"
        finish_membership_change(session, change)
        with pytest.raises(ValueError, match="account_group_member_purpose_mismatch"):
            freeze_membership_snapshot(session, task, participation_unit="first")


@pytest.mark.parametrize("task_type", ["group_ai_chat", "channel_comment", "channel_like", "channel_view"])
def test_membership_wake_reaches_current_owner_once(task_type):
    with _session() as session:
        _seed(session)
        pair = _initialize(session)[0]
        task = _task(session, task_type=task_type)
        paused = _task(session, status="paused")
        unrelated = _task(session, pool_id=2)
        snapshot = freeze_membership_snapshot(session, task, participation_unit="first")
        wake = session.scalar(select(StageWakeOutbox).where(
            StageWakeOutbox.aggregate_id == pair.membership.id))
        assert wakes.consume_membership_wake(session, wake.id, _now()) == 1
        assert wakes.consume_membership_wake(session, wake.id, _now()) == 0
        state = session.scalar(select(TaskPlannerWakeState).where(TaskPlannerWakeState.task_id == task.id))
        assert state.wake_revision == 1 and state.lifecycle_epoch == task.task_lifecycle_epoch
        assert session.scalar(select(TaskPlannerWakeState).where(
            TaskPlannerWakeState.task_id.in_([paused.id, unrelated.id]))) is None
        assert snapshot.member_account_ids == [11, 12] and wake.state == "delivered"


def test_wake_transaction_rolls_back_all_delivery_when_one_task_fails(monkeypatch):
    with _session() as session:
        _seed(session)
        _initialize(session)
        _task(session)
        _task(session)
        session.commit()
        factory = sessionmaker(bind=session.get_bind())
        original = wakes.wake_task_planner
        calls = []
        def fail_second(db, task, **options):
            calls.append(task.id)
            if len(calls) % 2 == 0:
                raise ValueError("test delivery failure")
            return original(db, task, **options)
        monkeypatch.setattr(wakes, "wake_task_planner", fail_second)
        wakes.drain_membership_wake_transactions(factory)
        session.expire_all()
        assert session.scalar(select(func.count(TaskPlannerWakeState.id))) == 0
        pool_one_events = session.scalars(select(StageWakeOutbox).where(
            StageWakeOutbox.state == "pending")).all()
        assert len(pool_one_events) == 2


@pytest.mark.parametrize("binding_state", ["active", "scheduled"])
def test_current_formal_binding_protects_empty_group_from_deletion(binding_state):
    with _session() as session:
        _seed(session)
        _initialize(session)
        task = _task(session, pool_id=2)
        binding = session.scalar(select(TaskAccountGroupBindingSetRevision).where(
            TaskAccountGroupBindingSetRevision.task_id == task.id))
        binding.state = binding_state
        task.account_config = task.type_config = {}
        session.flush()
        with pytest.raises(ValueError, match="account_group_current_or_unsettled_binding"):
            assert_pool_can_be_deleted(session, session.get(AccountPool, 2))


def test_superseded_binding_with_unknown_cannot_be_deleted_after_task_stops():
    with _session() as session:
        _seed(session)
        _initialize(session)
        task = _task(session, pool_id=2, status="completed")
        binding = session.scalar(select(TaskAccountGroupBindingSetRevision).where(
            TaskAccountGroupBindingSetRevision.task_id == task.id))
        binding.state = "superseded"
        action = Action(tenant_id=1, task_id=task.id, task_type=task.type,
            action_type="like_message", account_id=11, status="failed")
        session.add(action)
        session.flush()
        attempt = ExecutionAttempt(tenant_id=1, action_id=action.id, account_id=11, status="result_unknown")
        session.add(attempt)
        session.flush()
        with pytest.raises(ValueError, match="account_group_current_or_unsettled_binding"):
            assert_pool_can_be_deleted(session, session.get(AccountPool, 2))
        attempt.status = "failed"
        session.flush()
        assert_pool_can_be_deleted(session, session.get(AccountPool, 2))


def test_legacy_multi_group_config_protects_empty_group():
    with _session() as session:
        _seed(session)
        session.add(Task(tenant_id=1, name="旧绑定", type="channel_view", status="running",
            account_config={"account_group_ids": [1, 2]}))
        session.flush()
        with pytest.raises(ValueError, match="running/paused"):
            assert_pool_can_be_deleted(session, session.get(AccountPool, 2))


@pytest.mark.parametrize("state", ["reserved", "call_issued", "remote_unknown"])
def test_unsettled_physical_lease_protects_pool_even_after_business_task_completed(state):
    with _session() as session:
        _seed(session)
        _initialize(session)
        task = _task(session, pool_id=2, status="completed")
        policy = session.scalar(select(AccountPoolConcurrencyPolicyRevision).where(
            AccountPoolConcurrencyPolicyRevision.account_pool_id == 2))
        action = Action(tenant_id=1, task_id=task.id, task_type=task.type,
            action_type="like_message", account_id=11, status="success")
        session.add(action)
        session.flush()
        attempt = ExecutionAttempt(tenant_id=1, action_id=action.id, account_id=11, status="success")
        session.add(attempt)
        session.flush()
        lease = AccountPoolConcurrencyLease(tenant_id=1, policy_revision_id=policy.id,
            account_pool_id=2, task_id=task.id, account_id=11, action_id=action.id,
            attempt_id=attempt.id, invocation_identity="test", task_group_share_limit=1, state=state)
        session.add(lease)
        session.flush()
        with pytest.raises(ValueError, match="account_group_unsettled_invocation"):
            assert_pool_can_be_deleted(session, session.get(AccountPool, 2))
        lease.state = "released"
        session.flush()
        assert_pool_can_be_deleted(session, session.get(AccountPool, 2))


def test_group_wake_does_not_revive_deleted_or_old_epoch_tasks():
    with _session() as session:
        _seed(session)
        pair = _initialize(session)[0]
        deleted = _task(session)
        deleted.deleted_at = _now()
        stale = _task(session)
        stale.task_lifecycle_epoch += 1
        session.flush()
        wake = session.scalar(select(StageWakeOutbox).where(StageWakeOutbox.aggregate_id == pair.state.id))
        assert wakes.consume_membership_wake(session, wake.id, _now()) == 1
        assert session.scalar(select(func.count(TaskPlannerWakeState.id))) == 0
