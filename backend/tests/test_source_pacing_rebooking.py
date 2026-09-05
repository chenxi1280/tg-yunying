"""Overdue reservations must not repeatedly converge on the same source slot."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import SourcePacingAdmission
from app.services.task_center.source_pacing_admission import admit_source_paced_attempt
from tests.test_source_pacing_owner_reuse import (
    NOW, SOURCE_GAP_SECONDS, _add_paced_action, _attempt, session,
)


pytestmark = pytest.mark.no_postgres


def test_overdue_waiters_receive_distinct_stable_slots(session):
    actions = [_add_paced_action(session, f"task-{i}", f"slot-{i}", f"action-{i}")
               for i in range(4)]
    for action, attempt in actions:
        assert not admit_source_paced_attempt(session, action, attempt,
                                             now_value=NOW - timedelta(seconds=1))
    overdue = NOW + timedelta(seconds=SOURCE_GAP_SECONDS * 4)
    for index, (action, _) in enumerate(actions):
        attempt = _attempt(action, 2, overdue)
        session.add(attempt)
        session.flush()
        admitted = admit_source_paced_attempt(session, action, attempt, now_value=overdue)
        assert admitted == (index == 0)
        if index:
            assert action.scheduled_at == overdue + timedelta(seconds=SOURCE_GAP_SECONDS * index)
    slots = list(session.scalars(select(SourcePacingAdmission).where(
        SourcePacingAdmission.state == "reserved").order_by(SourcePacingAdmission.call_not_before_at)))
    assert len(slots) == 3
    first_waiter = actions[1][0]
    prior_time = first_waiter.scheduled_at
    early_retry = _attempt(first_waiter, 3, overdue + timedelta(seconds=1))
    session.add(early_retry)
    session.flush()
    assert not admit_source_paced_attempt(session, first_waiter, early_retry,
                                         now_value=early_retry.before_call_at)
    assert first_waiter.scheduled_at == prior_time
