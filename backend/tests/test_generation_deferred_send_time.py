from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Task
from app.services._common import _now
from app.services.task_center.ai_generation_parallel import _candidate_statement
from app.services.task_center.ai_generation_timing import generation_not_before, generation_send_time_expression
from app.services.task_center.comment_generation_worker import _comment_candidate_statement
from tests.test_ai_generation_candidate_fairness import _seed_candidate, session


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("task_type", ["group_ai_chat", "channel_comment"])
@pytest.mark.parametrize("latest_field", ["scheduled_at", "release_not_before_at", "effective_claim_at"])
def test_deferred_send_does_not_enter_either_generation_queue(session, task_type, latest_field):
    action = _seed_candidate(session, "deferred")
    task = session.get(Task, action.task_id)
    task.type = task_type
    action.task_type = task_type
    action.action_type = "post_comment" if task_type == "channel_comment" else "send_message"
    now = _now()
    action.scheduled_at = now - timedelta(hours=1)
    action.release_not_before_at = now - timedelta(minutes=30)
    action.effective_claim_at = now - timedelta(minutes=20)
    expected = now + timedelta(hours=2)
    setattr(action, latest_field, expected)
    session.commit()
    query = _comment_candidate_statement(session) if task_type == "channel_comment" else _candidate_statement(1)
    assert list(session.scalars(query)) == []
    assert generation_not_before(action) == expected - timedelta(seconds=10)
    assert session.scalar(select(generation_send_time_expression()).where(type(action).id == action.id)) == expected
    assert getattr(action, latest_field) == expected


def test_optional_send_limits_do_not_replace_the_required_schedule(session):
    action = _seed_candidate(session, "scheduled")
    expected = action.scheduled_at
    assert action.effective_claim_at is None and action.release_not_before_at is None
    assert generation_not_before(action) == expected - timedelta(seconds=10)
    assert session.scalar(select(generation_send_time_expression())) == expected
