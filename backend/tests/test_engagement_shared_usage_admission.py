from datetime import timedelta

import pytest
from sqlalchemy import event, select

from app.models import (
    AccountBehaviorBudgetLedger, AccountBehaviorBudgetPolicyRevision,
    AccountBehaviorBudgetReservation, AccountPool, AccountPoolConcurrencyLease,
    AccountPoolConcurrencyPolicyRevision, RemoteInvocationFence,
    TaskAccountGroupBindingSetRevision, TgAccount,
)
from app.services.task_center import engagement_runtime_resources as resources
from app.services.task_center import dispatcher
from app.services.task_center.daily_ledgers import ensure_task_day_ledger
from app.services.task_center.engagement_participation import ensure_source_participation_plan
from app.services.task_center.engagement_runtime_capacity import _assert_pool_capacity
from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
from app.services.task_center.engagement_runtime_settlement import move_counter
from app.services.task_center.engagement_shared_usage import SharedUsageScope, read_shared_account_usage
from app.timezone import as_beijing
from tests.test_engagement_legacy_resource_origin import _cutover, _legacy_attempt
from tests.test_engagement_runtime_resources import _attempt, _seed, _session


pytestmark = pytest.mark.no_postgres
ACTION_BUDGETS = {"reaction": 10, "view": 10, "authored_message": 10, "authored_comment": 10}


def _budget(session, *, total=1, reaction=10):
    policy = session.scalar(select(AccountBehaviorBudgetPolicyRevision))
    policy.action_budgets = {**ACTION_BUDGETS, "total": total, "reaction": reaction}
    session.flush()


def _historical_call(session, task, *, original_at=None, called_at=None, state="success", ack=False):
    action, attempt = _attempt(session, task, 11, action_type="view_message")
    called_at = called_at or resources._now()
    action.pacing_due_at = original_at or called_at
    attempt.gateway_call_started_at = called_at
    attempt.after_call_at = called_at + timedelta(seconds=1)
    attempt.status = state
    attempt.result_snapshot = {"transport_termination_state": "acknowledged"} if ack else {}
    session.flush()
    return action, attempt


def _confirm(session, action, attempt):
    resources.mark_attempt_call_issued(session, attempt)
    action.status = attempt.status = "success"
    resources.settle_attempt_resources(attempt, action, remote_mutation_started=True)
    session.flush()


def test_unreserved_old_task_call_occupies_actual_day_across_action_classes():
    with _session() as session:
        task = _seed(session)
        _budget(session)
        old = _historical_call(session, task, original_at=resources._now() - timedelta(days=1))
        frozen = (old[0].pacing_due_at, old[1].status, old[1].gateway_call_started_at)

        with pytest.raises(RuntimeResourceBlocked, match="account_behavior_total_budget_exhausted"):
            resources.reserve_attempt_resources(session, *_attempt(session, task, 11))

        assert frozen == (old[0].pacing_due_at, old[1].status, old[1].gateway_call_started_at)
        assert session.scalar(select(AccountBehaviorBudgetReservation)) is None


def test_unreserved_same_original_day_is_added_to_original_budget():
    with _session() as session:
        task = _seed(session)
        _budget(session)
        _historical_call(session, task)
        with pytest.raises(RuntimeResourceBlocked, match="account_behavior_total_budget_exhausted"):
            resources.reserve_attempt_resources(session, *_attempt(session, task, 11))


def test_reserved_old_day_call_does_not_escape_current_activity_limit():
    with _session() as session:
        task = _seed(session)
        _budget(session)
        _cutover(session, task)
        old = _legacy_attempt(session, task)
        resources.reserve_attempt_resources(session, *old)
        _confirm(session, *old)
        original = session.scalar(select(AccountBehaviorBudgetLedger))
        frozen = (original.id, original.task_day, dict(original.counters))
        ensure_source_participation_plan(session, task, ensure_task_day_ledger(session, task),
            source_identity="current-day-source", required_count=3)

        with pytest.raises(RuntimeResourceBlocked, match="account_behavior_total_budget_exhausted"):
            resources.reserve_attempt_resources(session, *_attempt(session, task, 11))

        assert frozen == (original.id, original.task_day, original.counters)


@pytest.mark.parametrize("ack", [False, True])
def test_old_unknown_physical_occupancy_survives_day_but_ack_releases_only_transport(ack):
    with _session() as session:
        task = _seed(session)
        old_at = resources._now() - timedelta(days=2)
        old = _historical_call(session, task, called_at=old_at, state="result_unknown", ack=ack)
        new = _attempt(session, task, 11)
        if ack:
            resources.reserve_attempt_resources(session, *new)
        else:
            with pytest.raises(RuntimeResourceBlocked, match="account_legacy_remote_inflight"):
                resources.reserve_attempt_resources(session, *new)
        assert old[1].status == "result_unknown"
        assert old[1].gateway_call_started_at == old_at


def test_missing_original_date_is_unproven_instead_of_zero_usage():
    with _session() as session:
        task = _seed(session)
        action, _ = _historical_call(session, task)
        action.pacing_due_at = None
        session.flush()
        with pytest.raises(RuntimeResourceBlocked, match="original_task_day_unproven"):
            resources.reserve_attempt_resources(session, *_attempt(session, task, 11))


def test_existing_reservation_is_not_added_as_legacy_usage_again():
    with _session() as session:
        task = _seed(session)
        _budget(session, total=2)
        first = _attempt(session, task, 11)
        resources.reserve_attempt_resources(session, *first)
        _confirm(session, *first)
        resources.reserve_attempt_resources(session, *_attempt(session, task, 11))
        ledger = session.scalar(select(AccountBehaviorBudgetLedger))
        assert ledger.counters["reaction"] == {"reserved": 1, "call_issued": 0, "confirmed": 1}


def test_other_account_historical_usage_does_not_consume_candidate_budget():
    with _session() as session:
        task = _seed(session)
        _budget(session)
        _historical_call(session, task)
        resources.reserve_attempt_resources(session, *_attempt(session, task, 12))


def test_account_inflight_check_follows_physical_account_after_move_to_another_pool():
    with _session() as session:
        task = _seed(session)
        resources.reserve_attempt_resources(session, *_attempt(session, task, 11))
        session.add(AccountPool(id=2, tenant_id=1, name="后来分组"))
        policy = AccountPoolConcurrencyPolicyRevision(tenant_id=1, account_pool_id=2,
            hard_remote_inflight_limit=2)
        session.add(policy)
        binding = session.scalar(select(TaskAccountGroupBindingSetRevision))
        binding.account_group_ids = [2]
        account = session.get(TgAccount, 11)
        account.pool_id = 2
        action, _ = _attempt(session, task, 11)
        session.flush()

        with pytest.raises(RuntimeResourceBlocked, match="account_remote_inflight"):
            _assert_pool_capacity(session, action, account=account, binding=binding,
                policy=policy, pool_id=2)


@pytest.mark.parametrize("occupied", [False, True])
def test_call_start_rechecks_actual_day_after_reservation_crosses_midnight(monkeypatch, occupied):
    with _session() as session:
        task = _seed(session)
        _budget(session)
        now = as_beijing(resources._now())
        before = now.replace(hour=23, minute=59, second=59, microsecond=0)
        after = before + timedelta(seconds=2)
        binding = session.scalar(select(TaskAccountGroupBindingSetRevision))
        binding.effective_from = before - timedelta(hours=1)
        action, attempt = _attempt(session, task, 11)
        action.created_at = before
        monkeypatch.setattr(resources, "_now", lambda: before)
        resources.reserve_attempt_resources(session, action, attempt)
        original = session.scalar(select(AccountBehaviorBudgetLedger))
        if occupied:
            _historical_call(session, task, called_at=after, original_at=before)
        monkeypatch.setattr(resources, "_now", lambda: after)

        if occupied:
            with pytest.raises(RuntimeResourceBlocked, match="account_behavior_total_budget_exhausted"):
                resources.mark_attempt_call_issued(session, attempt)
            assert attempt.gateway_call_started_at is None
            assert session.scalar(select(AccountPoolConcurrencyLease)).state == "reserved"
            assert session.scalar(select(RemoteInvocationFence)).started_at is None
        else:
            resources.mark_attempt_call_issued(session, attempt)
            assert as_beijing(attempt.gateway_call_started_at).date() == after.date()
        assert original.task_day == before.date()


def test_late_budget_rejection_defers_only_uncalled_attempt_and_never_reaches_gateway(monkeypatch):
    with _session() as session:
        task = _seed(session)
        _budget(session)
        action, attempt = _attempt(session, task, 11)
        resources.reserve_attempt_resources(session, action, attempt)
        old = _historical_call(session, task)
        calls = []

        def dispatch_to_gateway(current_session, _action, _context):
            dispatcher._mark_gateway_call_started(current_session, attempt)
            calls.append("gateway")

        monkeypatch.setattr(dispatcher, "_action_pre_dispatch_handled", lambda *_args: False)
        monkeypatch.setattr(dispatcher, "_dispatch_account", lambda *_args: session.get(TgAccount, 11))
        monkeypatch.setattr(dispatcher, "validate_action_payload", lambda *_args: {})
        monkeypatch.setattr(dispatcher, "_dispatch_validated_action", dispatch_to_gateway)
        result = dispatcher._dispatch_action(session, action,
            generation_dependencies=dispatcher.PRODUCTION_GENERATION_DEPENDENCIES,
            comment_generation_dependencies=dispatcher.PRODUCTION_COMMENT_GENERATION_DEPENDENCIES)
        session.flush()

        assert result and calls == [] and attempt.gateway_call_started_at is None
        assert attempt.status == "skipped_before_gateway"
        assert attempt.failure_type == "account_behavior_total_budget_exhausted"
        assert action.result["error_code"] == "account_behavior_total_budget_exhausted"
        assert session.scalar(select(AccountPoolConcurrencyLease)).state == "released"
        assert session.scalar(select(AccountBehaviorBudgetReservation)).state == "released"
        assert session.scalar(select(RemoteInvocationFence)).started_at is None
        assert old[1].status == "success"


@pytest.mark.parametrize("count", [1, 25])
def test_shared_usage_has_two_reads_without_flushing_pending_changes(count):
    with _session() as session:
        task = _seed(session)
        for _ in range(count):
            _historical_call(session, task)
        task.name = "pending change"
        day = as_beijing(resources._now()).date()
        statements = []
        connection = session.connection()

        def record(_connection, _cursor, statement, *_args):
            statements.append(statement.split()[0])

        event.listen(connection, "before_cursor_execute", record)
        try:
            result = read_shared_account_usage(session, SharedUsageScope(1, 11, day, day))
        finally:
            event.remove(connection, "before_cursor_execute", record)
        assert statements == ["SELECT", "SELECT"] and task in session.dirty
        assert result.original_extra == result.activity_occupied == (("view", count),)


def test_current_day_unowned_and_legacy_call_share_total_without_double_counting():
    with _session() as session:
        task = _seed(session)
        _budget(session, total=2)
        policy = session.scalar(select(AccountBehaviorBudgetPolicyRevision))
        session.add(AccountBehaviorBudgetLedger(tenant_id=1, account_id=11,
            task_day=as_beijing(resources._now()).date(), policy_revision_id=policy.id,
            action_budgets=policy.action_budgets, counters={"reaction": {"unowned": 1}}))
        _historical_call(session, task, original_at=resources._now() - timedelta(days=1))
        with pytest.raises(RuntimeResourceBlocked, match="account_behavior_total_budget_exhausted"):
            resources.reserve_attempt_resources(session, *_attempt(session, task, 11))


def test_call_start_preserves_pending_budget_updates_with_production_autoflush_disabled():
    with _session() as session:
        task = _seed(session)
        _budget(session)
        session.autoflush = False
        action, attempt = _attempt(session, task, 11)
        resources.reserve_attempt_resources(session, action, attempt)
        ledger = session.scalar(select(AccountBehaviorBudgetLedger))
        move_counter(ledger, "view", old_state=None, new_state="unowned")
        with pytest.raises(RuntimeResourceBlocked, match="account_behavior_total_budget_exhausted"):
            resources.mark_attempt_call_issued(session, attempt)
        assert ledger.counters["view"]["unowned"] == 1
        assert attempt.gateway_call_started_at is None


def test_started_attempt_is_not_marked_again_or_charged_twice():
    with _session() as session:
        task = _seed(session)
        action, attempt = _attempt(session, task, 11)
        resources.reserve_attempt_resources(session, action, attempt)
        resources.mark_attempt_call_issued(session, attempt)
        session.flush()
        before = (attempt.gateway_call_started_at,
            dict(session.scalar(select(AccountBehaviorBudgetLedger)).counters))
        with pytest.raises(RuntimeResourceBlocked, match="engagement_attempt_already_called"):
            resources.mark_attempt_call_issued(session, attempt)
        assert before == (attempt.gateway_call_started_at,
            session.scalar(select(AccountBehaviorBudgetLedger)).counters)


@pytest.mark.parametrize("mutated", [False, True])
def test_failed_call_charges_only_proven_mutation_and_never_becomes_business_success(mutated):
    with _session() as session:
        task = _seed(session)
        _budget(session)
        _cutover(session, task)
        action, attempt = _legacy_attempt(session, task)
        resources.reserve_attempt_resources(session, action, attempt)
        resources.mark_attempt_call_issued(session, attempt)
        action.status = attempt.status = "failed"
        for _ in range(2):
            resources.settle_attempt_resources(attempt, action, remote_mutation_started=mutated)
        reservation = session.scalar(select(AccountBehaviorBudgetReservation))
        ledger = session.get(AccountBehaviorBudgetLedger, reservation.ledger_id)
        fence = session.scalar(select(RemoteInvocationFence))
        assert reservation.state == ("confirmed" if mutated else "released")
        assert ledger.counters["reaction"].get("confirmed", 0) == int(mutated)
        assert ledger.task_day == as_beijing(action.pacing_due_at).date()
        assert action.status == attempt.status == "failed"
        assert fence.business_outcome_state == ("failed" if mutated else "safely_not_called")
        ensure_source_participation_plan(session, task, ensure_task_day_ledger(session, task),
            source_identity="new-day-after-failure", required_count=3)
        current = _attempt(session, task, 11)
        if mutated:
            with pytest.raises(RuntimeResourceBlocked, match="account_behavior_total_budget_exhausted"):
                resources.reserve_attempt_resources(session, *current)
        else:
            resources.reserve_attempt_resources(session, *current)
