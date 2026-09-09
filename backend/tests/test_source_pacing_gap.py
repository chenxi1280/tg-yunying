"""AI source reservations use real available gaps within frozen windows."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    AccountBehaviorBudgetPolicyRevision, AccountBehaviorSessionPlan,
    SourcePacingAdmission, SourcePacingState, Task, TaskDayLedger,
)
from app.services.task_center.source_pacing_admission import admit_source_paced_attempt
from app.services.task_center.source_pacing_gap import earliest_source_gap
from tests.test_source_pacing_owner_reuse import (
    ACCOUNT_ID, NOW, SOURCE_GAP_SECONDS, TENANT_ID,
    _add_paced_action, _attempt, session,
)


pytestmark = pytest.mark.no_postgres
GAP = timedelta(seconds=SOURCE_GAP_SECONDS)


def _action(session, suffix):
    return _add_paced_action(session, f"gap-{suffix}", f"slot-{suffix}", f"action-{suffix}")


def _reserve(session, suffix):
    action, attempt = _action(session, suffix)
    assert not admit_source_paced_attempt(session, action, attempt, now_value=NOW - GAP)
    return action, session.scalar(select(SourcePacingAdmission).where(
        SourcePacingAdmission.action_id == action.id,
    ))


def _windows(session, action, windows):
    task = session.get(Task, action.task_id)
    task.type_config = {"engagement_contract_version": "unified_engagement_v1"}
    session.add(AccountBehaviorBudgetPolicyRevision(
        id="gap-policy", tenant_id=TENANT_ID, account_class="normal",
        action_budgets={}, session_budget={}, pair_gap_policy={}, wake_budget=0,
    ))
    session.flush()
    session.add(AccountBehaviorSessionPlan(
        tenant_id=TENANT_ID, account_id=ACCOUNT_ID, task_day=NOW.date(),
        policy_revision_id="gap-policy", chronotype="balanced", weekday_class="weekday",
        windows=[{"start_at": start.isoformat(), "end_at": end.isoformat()}
                 for start, end in windows], seed="gap-test",
    ))
    session.flush()


def test_stale_tail_does_not_displace_new_ai_request(session):
    old, admission = _reserve(session, "old-tail")
    old.status = "success"
    state = session.get(SourcePacingState, admission.source_pacing_state_id)
    state.next_call_not_before_at = NOW + timedelta(days=3)
    action, attempt = _action(session, "new-tail")

    assert admit_source_paced_attempt(session, action, attempt, now_value=NOW)
    assert state.last_call_started_at == NOW
    assert admission.state == "reserved"
    assert admission.action_id == old.id


@pytest.mark.parametrize("invalid", ["terminal", "old_epoch", "closed_day", "deadline", "stopped"])
def test_invalid_future_reservation_does_not_consume_available_gap(session, invalid):
    old, admission = _reserve(session, invalid)
    admission.call_not_before_at = NOW + GAP / 2
    task = session.get(Task, old.task_id)
    ledger = session.get(TaskDayLedger, admission.pacing_period_key)
    if invalid == "terminal":
        old.status = "success"
    elif invalid == "old_epoch":
        task.task_lifecycle_epoch += 1
    elif invalid == "closed_day":
        ledger.lifecycle_status = "closed"
    elif invalid == "deadline":
        # Ledgers use UTC storage, source reservation dates use Beijing wall time.
        ledger.deadline_at = NOW - timedelta(hours=8)
    else:
        task.status = "stopped"
    action, attempt = _action(session, f"after-{invalid}")

    assert admit_source_paced_attempt(session, action, attempt, now_value=NOW)
    assert admission.call_not_before_at == NOW + GAP / 2
    assert admission.state == "reserved"


def test_new_point_keeps_both_sides_of_larger_future_gap(session):
    _, other = _reserve(session, "large-gap")
    other.call_not_before_at = NOW + GAP
    other.source_gap_seconds = SOURCE_GAP_SECONDS * 2
    action, attempt = _action(session, "large-gap-current")

    assert not admit_source_paced_attempt(session, action, attempt, now_value=NOW)
    assert action.scheduled_at == NOW + GAP * 3
    assert other.call_not_before_at == NOW + GAP


def test_distant_large_gap_can_cover_earlier_small_reservation():
    peers = ((NOW + GAP, SOURCE_GAP_SECONDS),
             (NOW + GAP * 2, SOURCE_GAP_SECONDS * 3))
    actual = earliest_source_gap(
        NOW, peers, gap_seconds=SOURCE_GAP_SECONDS,
        deadline=NOW + GAP * 10, window_floor=lambda at: at,
    )
    assert actual == NOW + GAP * 5


def test_source_displacement_at_window_end_uses_next_original_window(session):
    first, first_attempt = _action(session, "window-first")
    assert admit_source_paced_attempt(session, first, first_attempt, now_value=NOW)
    action, attempt = _action(session, "window-current")
    next_start = NOW + GAP * 3
    _windows(session, action, [(NOW, NOW + GAP), (next_start, next_start + GAP)])

    assert not admit_source_paced_attempt(session, action, attempt, now_value=NOW)
    assert action.scheduled_at == next_start
    assert session.scalar(select(func.count(AccountBehaviorSessionPlan.id))) == 1
    assert action.pacing_due_at == NOW


def test_window_shift_is_checked_against_future_source_reservation(session):
    _, other = _reserve(session, "window-peer")
    other.call_not_before_at = NOW + GAP * 3
    action, attempt = _action(session, "window-cross")
    _windows(session, action, [(NOW + GAP * 3, NOW + GAP * 6)])

    assert not admit_source_paced_attempt(session, action, attempt, now_value=NOW)
    assert action.scheduled_at == NOW + GAP * 4
    assert other.call_not_before_at == NOW + GAP * 3


def test_no_remaining_window_is_explicit_and_does_not_create_a_plan(session):
    action, attempt = _action(session, "no-window")
    session.get(Task, action.task_id).type_config = {
        "engagement_contract_version": "unified_engagement_v1",
    }

    assert not admit_source_paced_attempt(session, action, attempt, now_value=NOW)
    assert attempt.failure_type == "pacing_source_period_exhausted"
    assert session.scalar(select(func.count(AccountBehaviorSessionPlan.id))) == 0
    assert session.scalar(select(SourcePacingState)).last_call_started_at is None


def test_expired_deadline_cannot_mark_call_started(session):
    action, attempt = _action(session, "expired")
    expired = NOW + timedelta(days=2)

    assert not admit_source_paced_attempt(session, action, attempt, now_value=expired)
    assert attempt.failure_type == "pacing_source_period_exhausted"
    assert session.scalar(select(SourcePacingState)).last_call_started_at is None


def test_unknown_preserves_identity_and_actual_gap_for_other_action(session):
    original, first_attempt = _action(session, "unknown")
    assert admit_source_paced_attempt(session, original, first_attempt, now_value=NOW)
    first_attempt.gateway_call_started_at = NOW
    first_attempt.status = "result_unknown"
    original.status = "unknown_after_send"
    admission = session.scalar(select(SourcePacingAdmission))
    admission.state = "remote_unknown"
    state = session.get(SourcePacingState, admission.source_pacing_state_id)
    state.last_source_gap_seconds = SOURCE_GAP_SECONDS * 2
    before = (admission.id, admission.action_id, admission.attempt_id, admission.state)
    next_action, next_attempt = _action(session, "after-unknown")

    assert not admit_source_paced_attempt(session, next_action, next_attempt, now_value=NOW)
    assert next_action.scheduled_at == NOW + GAP * 2
    assert before == (admission.id, admission.action_id, admission.attempt_id, admission.state)
    retry = _attempt(original, 2, NOW + GAP * 3)
    session.add(retry)
    session.flush()
    assert not admit_source_paced_attempt(session, original, retry, now_value=NOW + GAP * 3)
    assert retry.failure_type == "pacing_source_prior_call_started"


def test_peer_outside_frozen_window_does_not_block_current_gap(session):
    old, admission = _reserve(session, "outside-window")
    admission.call_not_before_at = NOW + GAP / 2
    _windows(session, old, [(NOW, NOW + GAP / 4), (NOW + GAP * 3, NOW + GAP * 5)])
    action, attempt = _action(session, "after-outside-window")

    assert admit_source_paced_attempt(session, action, attempt, now_value=NOW)
    assert admission.call_not_before_at == NOW + GAP / 2


def test_changed_plan_hash_does_not_hold_stale_source_reservation(session):
    old, admission = _reserve(session, "old-plan")
    admission.call_not_before_at = NOW + GAP / 2
    old.pacing_plan_hash = "b" * 64
    action, attempt = _action(session, "after-old-plan")

    assert admit_source_paced_attempt(session, action, attempt, now_value=NOW)
    assert admission.state == "reserved"


def test_current_persisted_release_and_account_effective_are_not_rolled_back(session):
    action, attempt = _action(session, "keep-release")
    action.release_not_before_at = NOW + GAP * 2
    action.effective_claim_at = NOW + GAP * 3

    assert not admit_source_paced_attempt(session, action, attempt, now_value=NOW)
    assert action.scheduled_at == NOW + GAP * 3
    assert action.pacing_due_at == NOW
