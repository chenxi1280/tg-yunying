from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import Task
from app.schemas.task_center import ChannelCommentConfig, GroupAIChatConfig
from app.services.task_center.config_normalization import validated_type_config
from app.services.task_center.service import _task_create_payload_for_precheck

pytestmark = pytest.mark.no_postgres


def test_group_ai_config_rejects_removed_consecutive_burst_fields():
    with pytest.raises(ValidationError):
        GroupAIChatConfig(target_group_id=7, consecutive_message_enabled=True)  # type: ignore[call-arg]


def test_normalize_existing_group_task_drops_legacy_burst_fields():
    normalized = validated_type_config(
        "group_ai_chat",
        {
            "target_group_id": 7,
            "messages_per_round": 2,
            "messages_per_round_mode": "manual",
            "consecutive_message_enabled": True,
            "consecutive_message_min": 2,
            "consecutive_message_max": 4,
            "auto_follow_required_channel": False,
            "hourly_min_messages": 30,
        },
    )
    assert "consecutive_message_enabled" not in normalized
    assert "auto_follow_required_channel" not in normalized
    assert normalized["group_bot_admission_required"] is True
    assert normalized["reply_min_per_round"] == 1


def test_existing_group_task_precheck_normalizes_removed_fields() -> None:
    task = Task(
        id="legacy-group-ai-start",
        tenant_id=1,
        name="旧 AI 活群任务",
        type="group_ai_chat",
        status="stopped",
        type_config={
            "target_group_id": 7,
            "hourly_min_messages": 30,
            "consecutive_message_enabled": False,
            "consecutive_message_min": 2,
            "consecutive_message_max": 4,
            "consecutive_message_probability": 0.3,
            "auto_follow_required_channel": True,
        },
    )

    payload = _task_create_payload_for_precheck(task)

    assert "consecutive_message_enabled" not in payload
    assert "auto_follow_required_channel" not in payload
    assert payload["group_bot_admission_required"] is True


def test_group_ai_defaults_reply_min_and_admission():
    config = GroupAIChatConfig(target_group_id=7, hourly_min_messages=30)
    assert config.reply_min_per_round == 1
    assert config.group_bot_admission_required is True


def test_group_ai_normalization_preserves_controlled_legacy_prompt_route():
    normalized = validated_type_config(
        "group_ai_chat",
        {
            "target_group_id": 7,
            "adult_prompt_enabled": True,
            "content_route": "adult_service",
        },
    )

    assert normalized["adult_prompt_enabled"] is True
    assert normalized["content_route"] == "adult_service"


def test_group_ai_config_rejects_unknown_legacy_prompt_route():
    with pytest.raises(ValidationError):
        GroupAIChatConfig(target_group_id=7, content_route="unknown")  # type: ignore[arg-type]


def test_channel_comment_defaults_mixed_reply():
    config = ChannelCommentConfig(target_channel_id=9)
    assert config.comment_mode == "mixed"
    assert config.reply_min_per_message == 1
