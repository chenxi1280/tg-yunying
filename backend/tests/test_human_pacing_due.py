from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models import ChannelMessage, Task
from app.services.task_center.executors import channel_comment_budget, channel_like, channel_view
from app.services.task_center.executors.channel_comment_budget import MessageCommentPlanState
from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION
from app.services.task_center.pacing import cumulative_pacing_due


pytestmark = pytest.mark.no_postgres


def test_late_source_starts_at_zero_and_uses_full_day_denominator() -> None:
    day_start = datetime(2026, 8, 7)
    source_observed_at = datetime(2026, 8, 7, 21, 16)

    assert cumulative_pacing_due(
        796,
        {},
        anchor_at=source_observed_at,
        period_start_at=day_start,
        period_end_at=day_start + timedelta(days=1),
        now=source_observed_at,
    ) == 0
    assert cumulative_pacing_due(
        796,
        {},
        anchor_at=source_observed_at,
        period_start_at=day_start,
        period_end_at=day_start + timedelta(days=1),
        now=source_observed_at + timedelta(minutes=30),
    ) == 16


def test_source_rolling_window_spreads_lifetime_target_over_24_hours() -> None:
    anchor = datetime(2026, 8, 7, 21, 16)

    assert cumulative_pacing_due(
        80,
        {},
        anchor_at=anchor,
        period_start_at=anchor,
        period_end_at=anchor + timedelta(days=1),
        now=anchor + timedelta(hours=3),
    ) == 10


def test_comment_and_like_only_materialize_source_due_quantity() -> None:
    anchor = datetime(2026, 8, 7, 21, 16)
    now = anchor + timedelta(hours=3)
    task = _current_task("channel_comment", started_at=anchor - timedelta(hours=1))
    message = ChannelMessage(id=1, tenant_id=1, channel_target_id=1, message_id=1, created_at=anchor)

    comment_due = channel_comment_budget._message_comment_deficit(
        {"target_comments_per_message": 80, "comment_count_jitter": 0},
        MessageCommentPlanState(0, 0, 0),
        task=task,
        message=message,
        now=now,
    )
    task.type = "channel_like"
    like_due = channel_like._paced_like_target(task, message, 80, now=now)

    assert comment_due == 10
    assert like_due == 10


def test_view_late_source_only_materializes_natural_day_due_quantity() -> None:
    day_start = datetime(2026, 8, 7)
    source_observed_at = datetime(2026, 8, 7, 21, 16)
    task = _current_task("channel_view", started_at=datetime(2026, 8, 7, 18))
    message = ChannelMessage(
        id=1,
        tenant_id=1,
        channel_target_id=1,
        message_id=1,
        created_at=source_observed_at,
    )
    inputs = SimpleNamespace(
        ledger=SimpleNamespace(
            period_start_at=day_start,
            deadline_at=day_start + timedelta(days=1),
        ),
        now=source_observed_at + timedelta(minutes=30),
    )

    assert channel_view._current_view_due(task, inputs, message, 796) == 16


def _current_task(task_type: str, *, started_at: datetime) -> Task:
    return Task(
        id=f"paced-{task_type}",
        tenant_id=1,
        name="拟人节奏",
        type=task_type,
        status="running",
        fulfillment_contract_version=CURRENT_CONTRACT_VERSION,
        pacing_config={},
        stats={"started_at": started_at.isoformat()},
        created_at=started_at - timedelta(days=1),
    )
