from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import (AccountBehaviorBudgetPolicyRevision, AccountBehaviorBudgetReservation,
    AccountPoolConcurrencyLease, AccountPoolConcurrencyPolicyRevision,
    ExecutionResiliencePolicyRevision, RemoteInvocationFence)
from app.services._common import _now
from app.services.task_center import engagement_runtime_resources as runtime
from app.services.task_center.dispatcher import _release_dangling_engagement_leases
from app.services.task_center.engagement_lease_recovery import recover_settleable_leases
from test_engagement_runtime_resources import _session, _seed, _attempt

pytestmark = pytest.mark.no_postgres


def _inflight(session, task, account_id):
    action, attempt = _attempt(session, task, account_id)
    runtime.reserve_attempt_resources(session, action, attempt)
    runtime.mark_attempt_call_issued(session, attempt)
    action.status, attempt.status = "executing", "gateway_call_started"
    attempt.gateway_call_started_at = _now()
    session.flush()
    return action, attempt


def _resources(session, attempt):
    return tuple(session.scalar(select(model).where(model.attempt_id == attempt.id)) for model in (
        AccountPoolConcurrencyLease, AccountBehaviorBudgetReservation, RemoteInvocationFence))


@pytest.mark.parametrize("action_status", ["executing", "failed", "cancelled"])
def test_unresolved_call_is_not_released_by_age_or_action_status(action_status):
    with _session() as session:
        task = _seed(session)
        action, attempt = _inflight(session, task, 11)
        action.status = action_status
        lease, budget, fence = _resources(session, attempt)
        lease.acquired_at = _now() - timedelta(hours=1)
        assert runtime.recover_stale_concurrency_leases(session) == 0
        _release_dangling_engagement_leases(session, action)
        assert (lease.state, budget.state, fence.state) == ("call_issued", "call_issued", "active")
        assert fence.transport_termination_state != "acknowledged"


def test_held_unknown_does_not_starve_following_terminal_lease():
    with _session() as session:
        task = _seed(session)
        action, attempt = _inflight(session, task, 11)
        action.status, attempt.status = "unknown_after_send", "result_unknown"
        runtime.settle_attempt_resources(attempt, action, remote_mutation_started=True)
        held, _, _ = _resources(session, attempt)
        held.id = "000-held"
        other, terminal = _inflight(session, task, 12)
        other.status = terminal.status = "failed"
        last, budget, fence = _resources(session, terminal)
        last.id = "zzz-ready"
        assert runtime.recover_stale_concurrency_leases(session, limit=1) == 1
        assert held.state == "remote_unknown"
        assert (last.state, budget.state, fence.state) == ("released", "released", "terminal")
        assert runtime.recover_stale_concurrency_leases(session, limit=1) == 0


def test_late_transport_ack_releases_capacity_but_keeps_business_unknown():
    with _session() as session:
        task = _seed(session)
        action, attempt = _inflight(session, task, 11)
        attempt.status, action.status = "result_unknown", "unknown_after_send"
        runtime.settle_attempt_resources(attempt, action, remote_mutation_started=True)
        attempt.result_snapshot = {**attempt.result_snapshot, "transport_termination_state": "acknowledged"}
        assert runtime.recover_stale_concurrency_leases(session) == 1
        lease, budget, fence = _resources(session, attempt)
        assert lease.state == "released" and budget.state == "unknown"
        assert fence.state == "remote_unknown" and fence.business_outcome_state == "unknown"
        attempt.result_snapshot = {}
        runtime.settle_attempt_resources(attempt, action, remote_mutation_started=True)
        assert lease.state == "released" and fence.transport_termination_state == "acknowledged"


def test_terminal_action_before_gateway_releases_only_unissued_reservation():
    with _session() as session:
        task = _seed(session)
        action, attempt = _attempt(session, task, 11)
        runtime.reserve_attempt_resources(session, action, attempt)
        action.status = "failed"
        assert runtime.recover_stale_concurrency_leases(session) == 1
        lease, budget, fence = _resources(session, attempt)
        assert (lease.state, budget.state, fence.business_outcome_state) == ("released", "released", "safely_not_called")
        assert attempt.status == "skipped_before_gateway" and attempt.gateway_call_started_at is None


def test_recovery_error_rolls_back_only_one_attempt_and_is_not_counted(caplog):
    with _session() as session:
        task = _seed(session)
        first, failed = _inflight(session, task, 11)
        second, healthy = _inflight(session, task, 12)
        first.status = second.status = failed.status = healthy.status = "failed"
        session.commit()
        def settlement(attempt, action, **kwargs):
            runtime.settle_attempt_resources(attempt, action, **kwargs)
            if attempt.id == failed.id:
                raise ValueError("test_failed_settlement")
        assert recover_settleable_leases(session, limit=2, settle=settlement) == 1
        assert _resources(session, failed)[0].state == "call_issued"
        assert _resources(session, healthy)[0].state == "released"
        assert "engagement_lease_recovery_failed" in caplog.text


def test_recovery_locks_resilience_before_budget_ledger(monkeypatch):
    from app.services.task_center import engagement_runtime_settlement as settlement
    order = []
    policy_lock, ledger_lock = settlement.lock_resilience_policy, settlement.locked_ledger_by_id
    def policy(session, identity):
        order.append("policy")
        return policy_lock(session, identity)
    def ledger(session, identity):
        order.append("ledger")
        return ledger_lock(session, identity)
    with _session() as session:
        task = _seed(session)
        action, attempt = _inflight(session, task, 11)
        action.status = attempt.status = "success"
        session.commit()
        monkeypatch.setattr(settlement, "lock_resilience_policy", policy)
        monkeypatch.setattr(settlement, "locked_ledger_by_id", ledger)
        assert runtime.recover_stale_concurrency_leases(session) == 1
        assert order == ["policy", "ledger"]


POLICIES = (
    (AccountPoolConcurrencyPolicyRevision, runtime._active_pool_policy, (1, 1), "hard_remote_inflight_limit", 9),
    (AccountBehaviorBudgetPolicyRevision, runtime._active_budget_policy, (1, "normal"), "action_budgets", {"total": 77}),
    (ExecutionResiliencePolicyRevision, runtime._active_resilience_policy, (1,), "circuit_open_seconds", 444),
)


@pytest.mark.parametrize("policy_case", POLICIES)
def test_retired_policy_has_idempotent_successor_with_original_configuration(policy_case):
    model, ensure, args, field, value = policy_case
    with _session() as session:
        _seed(session)
        previous = session.scalar(select(model))
        previous.state = "retired"
        setattr(previous, field, value)
        session.commit()
        current = ensure(session, *args)
        assert current.revision == 2 and current.state == "active"
        assert current.id != previous.id and getattr(current, field) == value
        assert ensure(session, *args).id == current.id


@pytest.mark.parametrize("policy_case", POLICIES)
def test_explicitly_disabled_policy_is_not_recreated(policy_case):
    model, ensure, args, _, _ = policy_case
    with _session() as session:
        _seed(session)
        session.scalar(select(model)).state = "disabled"
        session.commit()
        with pytest.raises(ValueError, match="runtime_policy_inactive"):
            ensure(session, *args)
        assert len(list(session.scalars(select(model)))) == 1
