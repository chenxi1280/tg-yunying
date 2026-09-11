"""Neutral local regressions for deadline admission; no remote calls."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import Action, ExecutionAttempt, SourcePacingAdmission, SourcePacingState
from app.services.task_center.source_pacing_admission import admit_source_paced_attempt
from app.services.task_center.source_pacing_cursor import admission_not_before
from app.services.task_center.source_pacing_reservation import SourceAdmissionSpec
from test_source_pacing_admission import (
    NOW, session, _reaction_source_entities, _reaction_action_and_reservation,
)

pytestmark = pytest.mark.no_postgres
CHANNEL_ACTIONS = (
    ("channel_view", "view_message", "view"),
    ("channel_like", "like_message", "reaction"),
    ("channel_comment", "post_comment", "comment"),
)


@pytest.mark.parametrize("task_type,action_type,domain", CHANNEL_ACTIONS)
@pytest.mark.parametrize("overflow", [0, 1])
def test_impossible_slot_does_not_advance_shared_cursor(task_type, action_type, domain, overflow):
    deadline = NOW + timedelta(hours=1)
    tail = deadline + timedelta(seconds=overflow)
    state = SourcePacingState(next_call_not_before_at=tail)
    admission = SourcePacingAdmission(call_not_before_at=NOW)
    spec = SourceAdmissionSpec(domain, "neutral-source", "neutral-owner", "owner", 1,
        "period", "plan", tail, deadline, 60)
    action = Action(id="neutral-action", task_type=task_type, action_type=action_type,
                    status="claiming", effective_claim_at=NOW)
    assert admission_not_before(action, state, session=SimpleNamespace(), admission=admission,
        spec=spec, created=True, timestamp=NOW) == tail
    assert state.next_call_not_before_at == tail


@pytest.mark.parametrize("late_seconds", [0, 1])
def test_originally_valid_slot_cannot_call_at_or_after_deadline(session, late_seconds):
    message, task, owner = _reaction_source_entities()
    action, reservation, deadline = _reaction_action_and_reservation(task, owner)
    # Retry status intentionally avoids the late-admission recovery path.
    action.status = "retryable_failed"
    attempt = ExecutionAttempt(tenant_id=1, action_id=action.id, account_id=1,
                               attempt_no=1, status="before_call")
    session.add_all([message, task, owner, action, reservation, attempt])
    session.flush()
    assert not admit_source_paced_attempt(session, action, attempt,
                                          now_value=deadline + timedelta(seconds=late_seconds))
    session.flush()
    admission = session.scalar(select(SourcePacingAdmission))
    state = session.scalar(select(SourcePacingState))
    assert admission.state == "cancelled_pre_gateway"
    assert state.next_call_not_before_at is None
    assert state.last_call_started_at is None
    assert attempt.gateway_call_started_at is None
    assert attempt.failure_type == "pacing_source_period_exhausted"
    assert action.result["error_code"] == "pacing_source_period_exhausted"


def test_repeated_impossible_admission_does_not_accumulate_tail(session):
    message, task, owner = _reaction_source_entities()
    action, reservation, deadline = _reaction_action_and_reservation(task, owner)
    action.effective_claim_at = deadline
    session.add_all([message, task, owner, action, reservation])
    session.flush()
    for number in (1, 2):
        attempt = ExecutionAttempt(tenant_id=1, action_id=action.id, account_id=1,
                                   attempt_no=number, status="before_call")
        session.add(attempt)
        session.flush()
        assert not admit_source_paced_attempt(session, action, attempt, now_value=NOW)
        session.flush()
        assert session.scalar(select(SourcePacingState)).next_call_not_before_at is None
        assert session.scalar(select(SourcePacingAdmission)).state == "cancelled_pre_gateway"
