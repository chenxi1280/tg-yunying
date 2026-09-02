from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.models import Action, ChannelMessage, Task
from app.services.task_center.ai_generator import (
    _is_adult_channel_context,
    _looks_like_bad_channel_comment,
    clean_channel_comment_contents,
)
from app.services.task_center.ai_limits import recommend_ai_limits
from app.services.task_center.config_normalization import validated_type_config
from app.services.task_center.executors.channel_comment_budget import (
    _message_comment_deficit,
    MessageCommentPlanState,
)
from app.services.task_center.executors.channel_comment_targets import _target_from_action
from app.services.task_center.executors.channel_comment_preparation import _relation_accounts
from app.services.task_center.pacing_quantity import deterministic_quantity_with_jitter
from app.services.task_center.source_pacing import rolling_source_window


pytestmark = pytest.mark.no_postgres


def test_channel_comment_defaults_and_normalization():
    config = validated_type_config("channel_comment", {"target_channel_id": 123})
    assert config["comment_count_jitter"] == 0.05
    assert config["rolling_window_days"] == 3
    assert config["daily_comment_cap"] == 0
    assert config["comment_mode"] == "mixed"

    # Explicit 3-day window configuration is preserved
    custom_config = validated_type_config("channel_comment", {"target_channel_id": 123, "rolling_window_days": 3})
    assert custom_config["rolling_window_days"] == 3


def test_route_v2_general_cannot_be_promoted_by_legacy_adult_flag() -> None:
    config = {
        "ai_content_route_v2_enabled": True,
        "content_route": "general",
        "adult_prompt_enabled": True,
    }

    assert _is_adult_channel_context(config) is False


def test_grounding_contract_requires_three_days_and_positive_daily_cap():
    base = {
        "target_channel_id": 123,
        "channel_comment_grounding_v1_enabled": True,
        "ai_two_stage_enabled": True,
        "ai_model": "generator-model",
        "ai_semantic_reviewer_model": "reviewer-model",
        "ai_content_route_v2_enabled": True,
        "ai_content_policy_version_id": "policy-v1",
        "ai_content_allowed_routes": ["general"],
        "unicode_emoji_enabled": True,
        "image_meme_enabled": False,
        "unicode_emoji_weight_bps": 10000,
        "image_meme_weight_bps": 0,
    }
    with pytest.raises(ValueError, match="channel_comment_daily_cap_required"):
        validated_type_config("channel_comment", base)
    with pytest.raises(ValueError, match="channel_comment_rolling_window_must_be_3_days"):
        validated_type_config(
            "channel_comment",
            {**base, "daily_comment_cap": 100, "rolling_window_days": 1},
        )


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
    # Channel comments use the three-day product contract by default.
    start, end = rolling_source_window(task, observed_at)
    assert end == observed_at + timedelta(days=3)

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
    message.created_at = datetime(2026, 8, 24, 12, 0, 0)
    message.published_at = datetime(2026, 8, 20, 12, 0, 0)

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


def test_current_contract_without_published_at_does_not_plan_from_observed_time():
    task = MagicMock(spec=Task)
    task.id = "comment-source-fact"
    task.type = "channel_comment"
    task.fulfillment_contract_version = "fact_first_v3"
    task.pacing_config = {}
    task.type_config = {"rolling_window_days": 3}
    task.stats = {}
    task.scheduled_start = None
    task.created_at = datetime(2026, 8, 20, 10, 0, 0)
    task.config_revision = 1
    message = MagicMock(spec=ChannelMessage)
    message.id = 41
    message.created_at = datetime(2026, 8, 24, 12, 0, 0)
    message.published_at = None

    deficit = _message_comment_deficit(
        {"target_comments_per_message": 10, "rolling_window_days": 3},
        MessageCommentPlanState(0, 0, 0),
        task=task,
        message=message,
        now=datetime(2026, 8, 25, 0, 0, 0),
    )

    assert deficit == 0
    assert task.last_error == "source_published_at_unproven"


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


def test_own_history_reply_never_uses_the_author_account() -> None:
    account = MagicMock(id=42)

    assert _relation_accounts(
        [account], {"source": "own_history", "author_account_id": 42},
    ) == []
