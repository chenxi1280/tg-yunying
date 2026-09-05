from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    AccountBehaviorBudgetLedger, AccountBehaviorBudgetPolicyRevision,
    AccountBehaviorBudgetReservation, AccountPool, AccountPoolConcurrencyLease,
    ChannelMessage, ReactionFulfillmentObligation, RemoteInvocationFence,
    TaskAccountGroupBindingSetRevision, TgAccount,
)
from app.services.task_center.daily_ledgers import ensure_task_day_ledger
from app.services.task_center.engagement_action_classes import ACTION_CLASS_BY_TYPE
from app.services.task_center.engagement_binding import freeze_membership_snapshot
from app.services.task_center.engagement_runtime_resources import (
    RuntimeResourceBlocked, mark_attempt_call_issued, reserve_attempt_resources,
    settle_attempt_resources,
)
from app.timezone import as_beijing
from tests.test_engagement_runtime_resources import _attempt, _seed, _session


pytestmark = pytest.mark.no_postgres


def _cutover(session, task):
    binding = session.scalar(select(TaskAccountGroupBindingSetRevision))
    snapshot = freeze_membership_snapshot(
        session, task, participation_unit=f"legacy_cutover:{binding.id}",
    )
    session.flush()
    return binding, snapshot


def _legacy_attempt(session, task):
    action, attempt = _attempt(session, task, 11)
    binding = session.scalar(select(TaskAccountGroupBindingSetRevision))
    original = as_beijing(binding.effective_from) - timedelta(days=1)
    action.created_at = original
    action.pacing_due_at = original
    return action, attempt


def _count(session, model):
    return session.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize("task_type,action_type", (
    ("group_ai_chat", "send_message"), ("channel_comment", "post_comment"),
    ("channel_like", "like_message"), ("channel_view", "view_message"),
))
def test_old_work_gets_complete_resources_without_rewriting_identity(task_type, action_type):
    with _session() as session:
        task = _seed(session)
        binding, snapshot = _cutover(session, task)
        task.type = task_type
        policy = session.scalar(select(AccountBehaviorBudgetPolicyRevision))
        policy.action_budgets = {"total": 10, **dict.fromkeys(ACTION_CLASS_BY_TYPE.values(), 10)}
        action, attempt = _legacy_attempt(session, task)
        action.task_type, action.action_type = task_type, action_type
        frozen = (action.created_at, action.pacing_due_at, action.scheduled_at, dict(action.payload))

        reserve_attempt_resources(session, action, attempt)

        assert [_count(session, model) for model in (
            AccountPoolConcurrencyLease, AccountBehaviorBudgetReservation, RemoteInvocationFence,
        )] == [1, 1, 1]
        assert frozen == (action.created_at, action.pacing_due_at, action.scheduled_at, action.payload)
        ledger = session.scalar(select(AccountBehaviorBudgetLedger))
        assert ledger.task_day == as_beijing(action.pacing_due_at).date()
        assert attempt.result_snapshot["engagement_membership_snapshot_set_id"] == snapshot.id
        assert attempt.result_snapshot["engagement_binding_set_revision_id"] == binding.id
        assert attempt.result_snapshot["engagement_account_pool_provenance"] == "legacy_cutover_snapshot"


def test_delayed_old_obligation_uses_cutover_origin_after_account_moves():
    with _session() as session:
        task = _seed(session)
        binding, _snapshot = _cutover(session, task)
        action, attempt = _legacy_attempt(session, task)
        source = ChannelMessage(tenant_id=1, channel_target_id=101, message_id=1)
        session.add(source)
        session.flush()
        obligation = ReactionFulfillmentObligation(
            tenant_id=1, task_id=task.id, channel_message_id=source.id, account_id=11,
            reaction_contract_version=1, task_lifecycle_epoch=task.task_lifecycle_epoch,
            created_at=action.created_at,
        )
        session.add(obligation)
        session.add(AccountPool(id=2, tenant_id=1, name="新组"))
        session.flush()
        action.created_at = as_beijing(binding.effective_from) + timedelta(seconds=1)
        action.payload = {"reaction_fulfillment_obligation_id": obligation.id}
        session.get(TgAccount, 11).pool_id = 2

        reserve_attempt_resources(session, action, attempt)

        assert session.scalar(select(AccountPoolConcurrencyLease)).account_pool_id == 1
        assert obligation.current_action_id is None
        assert action.payload == {"reaction_fulfillment_obligation_id": obligation.id}


@pytest.mark.parametrize("problem,code", (
    ("missing_date", "legacy_original_task_day_unproven"),
    ("missing_ledger", "legacy_original_task_day_ledger_invalid"),
    ("wrong_hash", "legacy_cutover_snapshot_hash_mismatch"),
    ("wrong_owner", "legacy_cutover_snapshot_owner_mismatch"),
    ("not_member", "legacy_cutover_account_not_frozen"),
))
def test_invalid_original_evidence_creates_no_resource_triplet(problem, code):
    with _session() as session:
        task = _seed(session)
        _binding, snapshot = _cutover(session, task)
        action, attempt = _legacy_attempt(session, task)
        if problem == "missing_date":
            action.pacing_due_at = None
        if problem == "missing_ledger":
            action.payload = {"task_day_ledger_id": "absent"}
        if problem == "wrong_hash":
            snapshot.member_union_hash = "wrong"
        if problem == "wrong_owner":
            snapshot.tenant_id = 2
        if problem == "not_member":
            action.account_id = attempt.account_id = 99
            session.add(TgAccount(id=99, tenant_id=1, pool_id=1,
                phone_masked="99", display_name="后来加入"))
        session.flush()

        with pytest.raises(RuntimeResourceBlocked, match=code):
            reserve_attempt_resources(session, action, attempt)

        assert [_count(session, model) for model in (
            AccountPoolConcurrencyLease, AccountBehaviorBudgetReservation, RemoteInvocationFence,
        )] == [0, 0, 0]


def test_original_task_day_ledger_wins_over_pacing_date():
    with _session() as session:
        task = _seed(session)
        _cutover(session, task)
        action, attempt = _legacy_attempt(session, task)
        ledger = ensure_task_day_ledger(session, task)
        action.payload = {"task_day_ledger_id": ledger.id}

        reserve_attempt_resources(session, action, attempt)

        budget = session.scalar(select(AccountBehaviorBudgetLedger))
        assert budget.task_day == ledger.obligation_local_date
        assert budget.task_day != as_beijing(action.pacing_due_at).date()


def test_legacy_unknown_keeps_budget_fence_and_physical_lease():
    with _session() as session:
        task = _seed(session)
        _cutover(session, task)
        action, attempt = _legacy_attempt(session, task)
        reserve_attempt_resources(session, action, attempt)
        mark_attempt_call_issued(session, attempt)
        action.status, attempt.status = "unknown_after_send", "result_unknown"

        settle_attempt_resources(attempt, action, remote_mutation_started=None)

        assert session.scalar(select(AccountPoolConcurrencyLease)).state == "remote_unknown"
        assert session.scalar(select(AccountBehaviorBudgetReservation)).state == "unknown"
        fence = session.scalar(select(RemoteInvocationFence))
        assert fence.state == "remote_unknown"
    assert fence.transport_terminated_at is None


@pytest.mark.parametrize("field,value", (
    ("status", "result_unknown"), ("account_id", 12),
    ("task_lifecycle_epoch", 2), ("gateway_call_started_at", "original_time"),
))
def test_existing_call_and_wrong_owner_are_never_backfilled(field, value):
    with _session() as session:
        task = _seed(session)
        _cutover(session, task)
        action, attempt = _legacy_attempt(session, task)
        setattr(attempt, field, action.created_at if value == "original_time" else value)
        expected = "legacy_resource_requires_uncalled_attempt" if field in (
            "status", "gateway_call_started_at") else "legacy_resource_attempt_owner_mismatch"
        frozen = (attempt.status, attempt.gateway_call_started_at, dict(attempt.result_snapshot))

        with pytest.raises(RuntimeResourceBlocked, match=expected):
            reserve_attempt_resources(session, action, attempt)

        assert frozen == (attempt.status, attempt.gateway_call_started_at, attempt.result_snapshot)
        assert _count(session, AccountPoolConcurrencyLease) == 0
        assert _count(session, AccountBehaviorBudgetReservation) == 0
        assert _count(session, RemoteInvocationFence) == 0


def test_binding_successor_does_not_replace_initial_legacy_origin():
    with _session() as session:
        task = _seed(session)
        original, snapshot = _cutover(session, task)
        action, attempt = _legacy_attempt(session, task)
        original.state = "superseded"
        session.flush()
        session.add(TaskAccountGroupBindingSetRevision(
            tenant_id=1, task_id=task.id, task_lifecycle_epoch=task.task_lifecycle_epoch,
            binding_set_revision=2, account_group_ids=[2], binding_set_hash="new-binding",
            effective_from=as_beijing(original.effective_from) + timedelta(seconds=1),
        ))
        task.type_config = {**task.type_config, "account_group_ids": [2]}
        session.flush()

        reserve_attempt_resources(session, action, attempt)

        assert attempt.result_snapshot["engagement_binding_set_revision_id"] == original.id
        assert attempt.result_snapshot["engagement_membership_snapshot_set_id"] == snapshot.id
        assert session.scalar(select(AccountPoolConcurrencyLease)).account_pool_id == 1
