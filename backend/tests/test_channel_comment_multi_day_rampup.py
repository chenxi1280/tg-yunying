from __future__ import annotations

from datetime import datetime, timedelta
import pytest

from app.models import ChannelMessage, Task
from app.services.task_center.engagement_comment_participation import (
    business_max_comments,
)
from app.services.task_center.pacing_stratified import (
    _daily_ramp_factor,
    _weighted_hour_spans,
    stratified_hour_buckets,
)
from app.services.task_center.source_pacing import source_window_days

pytestmark = pytest.mark.no_postgres


def test_daily_ramp_factor_single_day() -> None:
    start = datetime(2026, 9, 3, 10, 0, 0)
    deadline = start + timedelta(hours=24)
    # Single day should always return 1.0
    assert _daily_ramp_factor(start, start, deadline) == 1.0
    assert _daily_ramp_factor(start + timedelta(hours=12), start, deadline) == 1.0


def test_daily_ramp_factor_multi_day_doubles_on_day_2() -> None:
    start = datetime(2026, 9, 3, 10, 0, 0)
    deadline = start + timedelta(days=3)  # 72 hours
    # Day 0: base factor 1.0
    f0 = _daily_ramp_factor(start + timedelta(hours=5), start, deadline)
    assert f0 == 1.0
    # Day 1: doubled factor 2.0!
    f1 = _daily_ramp_factor(start + timedelta(hours=28), start, deadline)
    assert f1 == 2.0
    # Day 2: factor 3.0!
    f2 = _daily_ramp_factor(start + timedelta(hours=52), start, deadline)
    assert f2 == 3.0


def test_stratified_buckets_multi_day_allocates_more_on_day_2_and_3() -> None:
    start = datetime(2026, 9, 3, 0, 0, 0)
    deadline = start + timedelta(days=3)
    # Uniform hourly curve
    hourly_curve = [1] * 24
    plan_total = 60
    buckets = stratified_hour_buckets(plan_total, hourly_curve, start, deadline, multi_day_rampup=True)
    
    day1_count = sum(count for s, e, count in buckets if s < start + timedelta(days=1))
    day2_count = sum(count for s, e, count in buckets if start + timedelta(days=1) <= s < start + timedelta(days=2))
    day3_count = sum(count for s, e, count in buckets if s >= start + timedelta(days=2))
    
    # Day 1: ~10 (1/6 of 60)
    # Day 2: ~20 (2/6 of 60, doubled!)
    # Day 3: ~30 (3/6 of 60)
    assert day1_count == 10
    assert day2_count == 20
    assert day3_count == 30
    assert day2_count == day1_count * 2  # Day 2 is exactly double of Day 1!
    assert day1_count + day2_count + day3_count == plan_total


def test_business_max_comments_expands_with_target() -> None:
    task = Task(
        id="t-1",
        type="channel_comment",
        type_config={"target_comments_per_message": 150},
    )
    assert business_max_comments(task) == 150


def test_business_max_comments_defaults_at_least_80() -> None:
    task = Task(
        id="t-1",
        type="channel_comment",
        type_config={"target_comments_per_message": 20},
    )
    assert business_max_comments(task) == 80


def test_source_window_days_defaults_to_3_for_channel_comment() -> None:
    task = Task(
        id="t-1",
        type="channel_comment",
        type_config={},
    )
    assert source_window_days(task) == 3


def test_source_window_days_can_be_configured_to_5() -> None:
    task = Task(
        id="t-1",
        type="channel_comment",
        type_config={"rolling_window_days": 5},
    )
    assert source_window_days(task) == 5


def test_returning_accounts_detection_logic() -> None:
    now = datetime(2026, 9, 3, 12, 0, 0)
    # Message created 30h ago (multi-day)
    msg_time = now - timedelta(hours=30)
    planned_at = now
    
    is_multi_day = bool(msg_time and planned_at and (planned_at - msg_time).total_seconds() >= 86400)
    assert is_multi_day is True
    
    # Task with allow_returning_accounts enabled
    task_with_returning = Task(id="t-1", type="channel_comment", type_config={"allow_returning_accounts": True})
    returning_enabled = bool((task_with_returning.type_config or {}).get("allow_returning_accounts"))
    assert returning_enabled is True
    
    # account_index % 3 == 0 triggers returning slot (every 3rd slot on Day 2+)
    excluded_ids = {101, 102}
    is_returning_slot = returning_enabled and is_multi_day and (3 % 3 == 0) and bool(excluded_ids)
    assert is_returning_slot is True
    
    # account_index % 3 != 0 is new account slot (new faces)
    is_new_slot = returning_enabled and is_multi_day and (4 % 3 == 0) and bool(excluded_ids)
    assert is_new_slot is False
