from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.no_postgres

from app.models import Action, ChannelMessage, CommentFulfillmentObligation, Task, TgAccount
from app.services.task_center.ai_generator import (
    _looks_like_bad_channel_comment,
    clean_channel_comment_contents,
)
from app.services.task_center.ai_limits import recommend_ai_limits
from app.services.task_center.comment_fulfillment import clean_expired_comment_obligations
from app.services.task_center.config_normalization import validated_type_config
from app.services.task_center.executors.channel_comment_budget import (
    _current_day_comment_action_count,
    _message_comment_deficit,
    _remaining_current_day_budget,
    MessageCommentPlanState,
)
from app.services.task_center.executors.channel_comment_preparation import _prepared_slot
from app.services.task_center.executors.channel_comment_targets import _target_from_action
from app.services.task_center.pacing_progress import source_rolling_pacing_due
from app.services.task_center.pacing_quantity import deterministic_quantity_with_jitter
from app.services.task_center.source_pacing import rolling_source_window


def test_channel_comment_defaults_and_normalization():
    config = validated_type_config("channel_comment", {"target_channel_id": 123})
    assert config["comment_count_jitter"] == 0.05
    # Default remains 1 day to respect existing baseline contract
    assert config["rolling_window_days"] == 1
    assert config["daily_comment_cap"] == 0
    assert config["comment_mode"] == "mixed"

    # Explicit 3-day window configuration is preserved
    custom_config = validated_type_config("channel_comment", {"target_channel_id": 123, "rolling_window_days": 3})
    assert custom_config["rolling_window_days"] == 3


def test_channel_comment_coverage_recommendation_scales_for_large_pools():
    # Small pool
    rec_small = recommend_ai_limits("channel_comment", 5)
    assert rec_small["target_comments_per_message"] == 3

    # Medium pool (20 accounts -> 12)
    rec_20 = recommend_ai_limits("channel_comment", 20)
    assert rec_20["target_comments_per_message"] == 12

    # 100 accounts -> 60 (60.0%)
    rec_100 = recommend_ai_limits("channel_comment", 100)
    assert rec_100["target_comments_per_message"] == 60

    # 300 accounts -> 180 (60.0%) - no 80 cap truncation
    rec_300 = recommend_ai_limits("channel_comment", 300)
    assert rec_300["target_comments_per_message"] == 180

    # 580 accounts -> 348 (60.0%)
    rec_580 = recommend_ai_limits("channel_comment", 580)
    assert rec_580["target_comments_per_message"] == 348


def test_rolling_source_window_uses_configured_days():
    task = MagicMock(spec=Task)
    task.type = "channel_comment"
    task.pacing_config = {}
    task.type_config = {}
    task.stats = {}
    task.scheduled_start = None
    task.created_at = datetime(2026, 8, 20, 10, 0, 0)

    observed_at = datetime(2026, 8, 25, 12, 0, 0)
    # Default 1 day
    start, end = rolling_source_window(task, observed_at)
    assert end == observed_at + timedelta(days=1)

    # Configured 3 days
    task.type_config = {"rolling_window_days": 3}
    start3, end3 = rolling_source_window(task, observed_at)
    assert end3 == observed_at + timedelta(days=3)


def test_deterministic_jitter_is_stable_per_message():
    seed_1 = "comment:task-1:msg-100:1"
    res_1a = deterministic_quantity_with_jitter(20, 0.05, seed_id=seed_1)
    res_1b = deterministic_quantity_with_jitter(20, 0.05, seed_id=seed_1)
    assert res_1a == res_1b
    assert 19 <= res_1a <= 21

    # Different message gets independent deterministic sample
    seed_2 = "comment:task-1:msg-101:1"
    res_2a = deterministic_quantity_with_jitter(20, 0.05, seed_id=seed_2)
    res_2b = deterministic_quantity_with_jitter(20, 0.05, seed_id=seed_2)
    assert res_2a == res_2b


def test_expired_window_returns_zero_deficit():
    task = MagicMock(spec=Task)
    task.type = "channel_comment"
    task.fulfillment_contract_version = "fact_first_v3"
    task.pacing_config = {}
    task.type_config = {"rolling_window_days": 3}
    task.stats = {}
    task.scheduled_start = None
    task.created_at = datetime(2026, 8, 20, 10, 0, 0)

    message = MagicMock(spec=ChannelMessage)
    message.created_at = datetime(2026, 8, 20, 12, 0, 0)

    # Now is 4 days later (expired window > 3 days)
    now = datetime(2026, 8, 25, 0, 0, 0)
    state = MessageCommentPlanState(reservation_count=0, next_slot_index=0, managed_collected_count=0)

    deficit = _message_comment_deficit(
        {"target_comments_per_message": 10, "rolling_window_days": 3},
        state,
        task=task,
        message=message,
        now=now,
    )
    assert deficit == 0


def test_anti_bot_comment_filters():
    bot_samples = [
        "太棒了！支持博主",
        "好文章，感谢分享！",
        "楼主辛苦了，支持一下",
        "好帖，收藏了",
        "博主写得真好",
        "学习了，必须支持",
    ]
    for sample in bot_samples:
        assert _looks_like_bad_channel_comment(sample) is True

    accepted = clean_channel_comment_contents(bot_samples)
    assert len(accepted) == 0

    valid_samples = [
        "这个价格包含了哪些服务呀？",
        "修图有点明显，不知道真人怎么样",
        "老哥求个具体位置或者价格参考",
        "周末去过一次，人确实挺多的",
    ]
    for sample in valid_samples:
        assert _looks_like_bad_channel_comment(sample) is False


def test_target_from_action_includes_author_account_id():
    action = MagicMock(spec=Action)
    action.account_id = 42
    action.payload = {
        "channel_message_id": 101,
        "comment_text": "真实老哥在此",
        "account_role": "测试账号",
    }
    target = _target_from_action(action, "999888")
    assert target is not None
    assert target["message_id"] == 999888
    assert target["author_account_id"] == 42
    assert target["preview"] == "真实老哥在此"
