from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.task_center.ai_generator import AiGenerationUnavailable
from app.services.task_center import ai_generator
from app.services.task_center.executors import group_ai_chat


pytestmark = pytest.mark.no_postgres


def test_group_ai_chat_normal_candidate_shortfall_is_visible_failure(monkeypatch):
    task = SimpleNamespace(tenant_id=1, stats={})

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        assert count == 3
        return ["只返回一条普通发言"], 0

    monkeypatch.setattr(group_ai_chat, "generate_group_messages", fake_generate_group_messages)

    with pytest.raises(AiGenerationUnavailable, match="AI 普通发言候选不足"):
        group_ai_chat._generate_group_planned_items(
            None,
            task,
            {},
            reply_targets=[],
            normal_count=3,
            target_label="测试群",
            history="真人用户: 今天群里有什么安排",
        )

    assert task.stats["normal_candidate_shortfall_count"] == 1


def test_group_ai_quality_fill_retries_until_shortfall_is_filled(monkeypatch):
    task = SimpleNamespace(tenant_id=1, stats={})
    rounds = [
        ["照片准", "照片没p"],
        ["价格问清楚了再说", "位置别乱发"],
    ]
    requested_counts: list[int] = []

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        requested_counts.append(count)
        return rounds.pop(0), 3

    monkeypatch.setattr(group_ai_chat, "generate_group_messages", fake_generate_group_messages)

    quality_items, tokens, stats = group_ai_chat._generate_quality_filled_items(
        None,
        task,
        {},
        reply_targets=[],
        normal_count=2,
        target_label="测试群",
        history="真人A: 昨天照片准",
        turn_count=2,
        duplicate_baseline_messages=["昨天照片准"],
        chat_mode=group_ai_chat.CHAT_MODE_REPLY,
        context_message_ids=[1],
        fact_anchor_required=False,
        low_confidence_silence_enabled=False,
        fill_reply_shortfall_with_normal=False,
        enable_quality_fallback=False,
    )

    assert requested_counts == [2, 2]
    assert [item["content"] for item in quality_items] == ["价格问清楚了再说", "位置别乱发"]
    assert [item["rewrite_attempts"] for item in quality_items] == [1, 1]
    assert tokens == 6
    assert stats["ai_generation_rounds"] == 2
    assert stats["quality_fill_rounds"] == 1


def test_group_ai_expires_profileless_actions_before_duplicate_baseline():
    source_path = Path(__file__).resolve().parents[1] / "app/services/task_center/executors/group_ai_chat.py"
    source = source_path.read_text()

    assert source.index("_expire_open_profileless_actions(") < source.index("planned_ai_messages = _recent_planned_ai_messages(")


def test_group_ai_quality_filter_records_rejection_samples():
    accepted, stats = group_ai_chat._quality_filter_ai_messages(
        ["照片准", "照片没p", "价格问清楚了再说"],
        ["昨天照片准"],
        chat_mode=group_ai_chat.CHAT_MODE_REPLY,
        anchor_message_ids=[1],
        fact_anchor_required=False,
        low_confidence_silence_enabled=False,
        limit=2,
    )

    assert [item["content"] for item in accepted] == ["价格问清楚了再说"]
    assert stats["quality_rejection_counts"]["duplicate_message"] == 2
    assert [item["content"] for item in stats["quality_rejection_samples"]] == ["照片准", "照片没p"]
    assert stats["ai_generation_candidate_count"] == 3


def test_group_ai_quality_filter_rejects_vague_ai_filler_before_memory():
    accepted, stats = group_ai_chat._quality_filter_ai_messages(
        ["这个确实不错", "感觉挺靠谱", "花花老师价格大概多少", "可以关注一下"],
        [],
        chat_mode=group_ai_chat.CHAT_MODE_REPLY,
        anchor_message_ids=[1],
        fact_anchor_required=False,
        low_confidence_silence_enabled=False,
        limit=2,
    )

    assert [item["content"] for item in accepted] == ["花花老师价格大概多少"]
    assert stats["quality_rejection_counts"]["template_shell_limited"] == 3
    assert [item["content"] for item in stats["quality_rejection_samples"]] == [
        "这个确实不错",
        "感觉挺靠谱",
        "可以关注一下",
    ]


def test_group_ai_quality_fill_uses_unique_emoji_after_three_failed_rounds(monkeypatch):
    task = SimpleNamespace(tenant_id=1, stats={})
    requested_counts: list[int] = []

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        requested_counts.append(count)
        return ["照片准", "照片没p"][:count], 2

    monkeypatch.setattr(group_ai_chat, "generate_group_messages", fake_generate_group_messages)

    quality_items, _tokens, stats = group_ai_chat._generate_quality_filled_items(
        None,
        task,
        {},
        reply_targets=[],
        normal_count=2,
        target_label="测试群",
        history="真人A: 昨天照片准",
        turn_count=2,
        duplicate_baseline_messages=["昨天照片准"],
        chat_mode=group_ai_chat.CHAT_MODE_REPLY,
        context_message_ids=[1],
        fact_anchor_required=False,
        low_confidence_silence_enabled=False,
        fill_reply_shortfall_with_normal=False,
        enable_quality_fallback=True,
    )

    assert requested_counts == [2, 2, 2]
    assert [item["quality_fallback"] for item in quality_items] == ["emoji_react", "emoji_react"]
    assert len({item["content"] for item in quality_items}) == 2
    assert stats["quality_fallback_count"] == 2


def test_group_ai_quality_fallback_avoids_recent_baseline_emojis(monkeypatch):
    task = SimpleNamespace(tenant_id=1, stats={})

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        return ["照片准", "照片没p"][:count], 2

    monkeypatch.setattr(group_ai_chat, "generate_group_messages", fake_generate_group_messages)
    monkeypatch.setattr(group_ai_chat.random, "sample", lambda pool, k: list(pool)[:k])

    quality_items, _tokens, stats = group_ai_chat._generate_quality_filled_items(
        None,
        task,
        {},
        reply_targets=[],
        normal_count=2,
        target_label="测试群",
        history="真人A: 昨天照片准",
        turn_count=2,
        duplicate_baseline_messages=["昨天照片准", "👍"],
        chat_mode=group_ai_chat.CHAT_MODE_REPLY,
        context_message_ids=[1],
        fact_anchor_required=False,
        low_confidence_silence_enabled=False,
        fill_reply_shortfall_with_normal=False,
        enable_quality_fallback=True,
    )

    assert [item["content"] for item in quality_items] == ["👌", "👀"]
    assert stats["quality_fallback_count"] == 2


def test_group_ai_chat_keeps_target_profile_out_of_fact_thread():
    config = group_ai_chat._generation_config_with_profile(
        {"active_topic_direction": {"title": "日常闲聊"}},
        {"1": "账号只聊已知经历"},
        {"1": "谨慎短句"},
        "真人A: 今天只聊停车位",
        "围绕停车位短句接话",
        {"profile_hit_summary": "常聊装修预算", "profile_version": 3, "profile_scene": "group_chat"},
    )

    assert config["topic_thread"] == "真人A: 今天只聊停车位"
    assert config["target_profile_style"] == "常聊装修预算"
    assert config["target_learning_profile"]["profile_version"] == 3


def test_group_ai_prompt_layers_target_profile_as_style_not_fact(monkeypatch):
    captured: dict[str, str] = {}

    def fake_generate_contents(_session, _tenant_id, *, requirements, **_kwargs):
        captured["requirements"] = requirements
        return ["停车位那句我懂"], 0

    monkeypatch.setattr(ai_generator, "generate_contents", fake_generate_contents)

    ai_generator.generate_group_messages(
        None,
        1,
        {
            "active_topic_direction": {"title": "日常闲聊"},
            "active_teacher_target": {"name": "花花老师", "description": "身材服务反馈"},
            "topic_thread": "真人A: 今天只聊停车位",
            "topic_plan": "只围绕停车位",
            "target_profile_style": "常聊装修预算",
            "account_profiles": {"7": "只讲自己知道的上下文"},
        },
        count=1,
        target_label="测试群",
        history="真人A: 今天只聊停车位",
    )

    assert "话题脉络：\n真人A: 今天只聊停车位" in captured["requirements"]
    assert "本轮话题方向：日常闲聊" in captured["requirements"]


def test_group_ai_prompt_includes_fixed_generation_slots(monkeypatch):
    captured: dict[str, str] = {}

    def fake_generate_contents(_session, _tenant_id, *, requirements, **_kwargs):
        captured["requirements"] = requirements
        return ["这句按一号短接", "这句按二号追问"], 0

    monkeypatch.setattr(ai_generator, "generate_contents", fake_generate_contents)

    ai_generator.generate_group_messages(
        None,
        1,
        {
            "active_teacher_target": {"name": "花花老师", "description": "身材服务反馈"},
            "target_profile_style": "常聊装修预算",
            "account_profiles": {"7": "只讲自己知道的上下文"},
            "generation_slots": [
                {
                    "slot_id": "task-1:cycle:1:turn:1",
                    "sequence_index": 1,
                    "account_id": 11,
                    "act_type": "short_react",
                    "account_profile": "青年短句，少表情，爱接别人话",
                },
                {
                    "slot_id": "task-1:cycle:1:turn:2",
                    "sequence_index": 2,
                    "account_id": 12,
                    "act_type": "light_question",
                    "account_profile": "中年谨慎，常追问价格和位置",
                },
            ],
        },
        count=2,
        target_label="测试群",
        history="真人A: 昨天照片准",
    )

    assert "固定发言 slots" in captured["requirements"]
    assert "slot 1：task-1:cycle:1:turn:1；账号 11；行为 short_react" in captured["requirements"]
    assert "青年短句，少表情，爱接别人话" in captured["requirements"]
    assert "slot 2：task-1:cycle:1:turn:2；账号 12；行为 light_question" in captured["requirements"]
    assert "中年谨慎，常追问价格和位置" in captured["requirements"]
    assert "讨论老师：花花老师\n对象说明：身材服务反馈" in captured["requirements"]
    assert "聊天对象" not in captured["requirements"]
    assert "全站目标画像（只作风格和话题参考，不能作为具体事实来源）：\n常聊装修预算" in captured["requirements"]
    assert "账号长期画像：\n- 账号 7: 只讲自己知道的上下文" in captured["requirements"]


def test_group_ai_reply_prompt_layers_target_profile_as_style_not_fact(monkeypatch):
    captured: dict[str, str] = {}

    def fake_generate_contents(_session, _tenant_id, *, requirements, **_kwargs):
        captured["requirements"] = requirements
        return ["这句我接一下"], 0

    monkeypatch.setattr(ai_generator, "generate_contents", fake_generate_contents)

    ai_generator.generate_group_reply_messages(
        None,
        1,
        {
            "active_topic_direction": {"title": "日常闲聊"},
            "active_teacher_target": {"name": "花花老师"},
            "target_profile_style": "常聊装修预算",
            "generation_slots": [
                {
                    "slot_id": "task-1:cycle:1:turn:1",
                    "sequence_index": 1,
                    "account_id": 11,
                    "act_type": "context_reply",
                    "account_profile": "只围绕引用短接一句",
                }
            ],
        },
        reply_targets=[{"author": "真人A", "preview": "停车位快没了", "source": "group"}],
        target_label="测试群",
        history="真人A: 停车位快没了",
    )

    assert "引用目标 1：作者：真人A；原文：停车位快没了；来源：group" in captured["requirements"]
    assert "本轮话题方向：日常闲聊" in captured["requirements"]
    assert "讨论老师：花花老师" in captured["requirements"]
    assert "slot 1：task-1:cycle:1:turn:1；账号 11；行为 context_reply" in captured["requirements"]
    assert "只围绕引用短接一句" in captured["requirements"]
    assert "聊天对象" not in captured["requirements"]
    assert "群聊上下文：\n真人A: 停车位快没了" in captured["requirements"]
    assert "全站目标画像（只作风格和话题参考，不能作为具体事实来源）：\n常聊装修预算" in captured["requirements"]


def test_group_ai_chat_reads_tenant_profile_without_target_identity():
    source = Path(group_ai_chat.__file__).read_text()

    assert "tenant_learning_profile_preview(session, task.tenant_id, GROUP_CHAT_SCENE)" in source
    assert "learning_profile_preview(session, task.tenant_id, task.target_id" not in source
    assert "target_id, GROUP_CHAT_SCENE" not in source
