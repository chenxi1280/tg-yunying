from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.task_center.ai_generator import AiGenerationUnavailable
from app.services.task_center import ai_generator
from app.services.task_center.executors import channel_comment


pytestmark = pytest.mark.no_postgres


def test_channel_comment_normal_candidate_shortfall_is_visible_failure(monkeypatch):
    task = SimpleNamespace(tenant_id=1, stats={})

    def fake_generate_channel_comments(_session, _tenant_id, _config, *, count, message_content, target_label):
        assert count == 3
        return ["只返回一条评论"], 0

    monkeypatch.setattr(channel_comment, "generate_channel_comments", fake_generate_channel_comments)

    with pytest.raises(AiGenerationUnavailable, match="AI 评论候选不足"):
        channel_comment._generate_normal_channel_comments(
            None,
            task,
            {},
            3,
            SimpleNamespace(content_preview="频道原文", message_url="https://t.me/c/1/2"),
            "测试频道",
        )

    assert task.stats["normal_candidate_shortfall_count"] == 1


def test_channel_comment_clean_rejects_provider_meta_content():
    contents = [
        "原材料内容明显是色情低俗内容 描述了性工作者和性行为",
        "飞机号是真的还是假的啊",
    ]

    assert ai_generator.clean_channel_comment_contents(contents) == ["飞机号是真的还是假的啊"]


def test_channel_comment_keeps_target_profile_out_of_comment_style():
    config = channel_comment._config_with_comment_profile(
        {"comment_style": "短评", "topic_hint": "频道消息"},
        {"profile_hit_summary": "读者喜欢追问尺寸", "profile_version": 5},
    )

    assert config["comment_style"] == "短评"
    assert config["target_comment_profile"] == "读者喜欢追问尺寸"


def test_channel_comment_prompt_layers_target_profile_as_style_not_fact(monkeypatch):
    captured: dict[str, str] = {}

    def fake_generate_contents(_session, _tenant_id, *, requirements, **_kwargs):
        captured["requirements"] = requirements
        return ["这个尺寸多少"], 0

    monkeypatch.setattr(ai_generator, "generate_contents", fake_generate_contents)

    ai_generator.generate_channel_comments(
        None,
        1,
        {
            "comment_style": "短评",
            "target_comment_profile": "读者喜欢追问尺寸",
        },
        count=1,
        message_content="频道原文只提到了手工成品",
        target_label="测试频道",
    )

    assert "频道消息：频道原文只提到了手工成品" in captured["requirements"]
    assert "评论风格：短评" in captured["requirements"]
    assert "全站目标画像（只作读者口吻和追问方式参考，不能作为具体事实来源）：\n读者喜欢追问尺寸" in captured["requirements"]


def test_channel_reply_prompt_layers_target_profile_as_style_not_fact(monkeypatch):
    captured: dict[str, str] = {}

    def fake_generate_contents(_session, _tenant_id, *, requirements, **_kwargs):
        captured["requirements"] = requirements
        return ["这个尺寸多少"], 0

    monkeypatch.setattr(ai_generator, "generate_contents", fake_generate_contents)

    ai_generator.generate_channel_reply_comments(
        None,
        1,
        {
            "comment_style": "短评",
            "target_comment_profile": "读者喜欢追问尺寸",
        },
        reply_targets=[{"author": "读者A", "preview": "这个手工成品不错", "source": "discussion"}],
        message_content="频道原文只提到了手工成品",
        target_label="测试频道",
    )

    assert "频道消息：频道原文只提到了手工成品" in captured["requirements"]
    assert "引用目标 1：作者：读者A；原文：这个手工成品不错；来源：discussion" in captured["requirements"]
    assert "全站目标画像（只作读者口吻和追问方式参考，不能作为具体事实来源）：\n读者喜欢追问尺寸" in captured["requirements"]


def test_channel_comment_reads_tenant_profile_without_target_identity():
    source = Path(channel_comment.__file__).read_text()

    assert "tenant_learning_profile_preview(session, task.tenant_id, CHANNEL_COMMENT_SCENE)" in source
    assert "learning_profile_preview(session, task.tenant_id, task.target_id" not in source
    assert "target_id, CHANNEL_COMMENT_SCENE" not in source
