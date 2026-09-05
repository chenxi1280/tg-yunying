from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import TaskCommentCapacityPeriod, TaskCommentCapacityReservation
from app.services.task_center.channel_comment_capacity import remaining_comment_capacity
from channel_comment_planner_test_support import (
    STABLE_PLANNER_NOW,
    planner_session,
    seed_comment_task,
)


pytestmark = pytest.mark.no_postgres


def test_capacity_calendar_preserves_an_unused_day_without_consuming_capacity():
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=3)
        first_at = STABLE_PLANNER_NOW
        last_at = first_at + timedelta(days=2)
        for at in (first_at, last_at):
            assert remaining_comment_capacity(session, task, 1, at=at) == 1
        periods = list(session.scalars(select(TaskCommentCapacityPeriod).order_by(
            TaskCommentCapacityPeriod.period_start_at,
        )))

        assert len(periods) == 3
        assert {period.capacity_limit for period in periods} == {1}
        assert periods[0].period_end_at == periods[1].period_start_at
        assert periods[1].period_end_at == periods[2].period_start_at
        assert session.scalar(select(TaskCommentCapacityReservation.id)) is None
        assert remaining_comment_capacity(session, task, 1, at=last_at) == 1
        assert list(session.scalars(select(TaskCommentCapacityPeriod))) == periods
