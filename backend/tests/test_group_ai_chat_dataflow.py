import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.task_center.ai_generator import AiGenerationUnavailable
from app.services.task_center import ai_generator
from app.services.task_center.executors import group_ai_chat


pytestmark = pytest.mark.no_postgres


def test_group_ai_generated_slots_use_prd_act_type_vocabulary():
    accounts = [SimpleNamespace(id=account_id) for account_id in [11, 12, 13, 14, 15]]

    slots = group_ai_chat._generation_slots_for_plan(
        cycle_id="task-1:cycle:1",
        accounts=accounts,
        turn_count=5,
        reply_targets=[],
        account_prompt_profiles={},
        allow_account_repeat=False,
    )

    act_types = [slot["act_type"] for slot in slots]
    assert act_types == ["short_react", "detail_follow", "question", "light_disagree", "topic_shift"]
    assert "light_question" not in act_types
    assert "side_comment" not in act_types


def test_group_ai_clean_rejects_provider_refusal_meta_content():
    contents = [
        "我无法为这个请求提供帮助 服务稳不稳",
        "不敢随便冲 价格咋说",
    ]

    assert ai_generator.clean_group_chat_contents(contents) == ["不敢随便冲 价格咋说"]


def test_group_ai_generated_slots_rotate_topic_and_teacher_targets():
    accounts = [SimpleNamespace(id=account_id) for account_id in [11, 12, 13]]

    slots = group_ai_chat._generation_slots_for_plan(
        cycle_id="task-1:cycle:1",
        accounts=accounts,
        turn_count=3,
        reply_targets=[],
        account_prompt_profiles={},
        allow_account_repeat=False,
        topic_directions=[
            {"title": "郑州楼凤妹子怎么样", "weight": 3},
            {"title": "主任最近约新妹子了", "weight": 2},
        ],
        teacher_targets=[
            {"name": "花花老师身材服务真好", "priority": 2},
            {"name": "新人榜单妹子", "priority": 1},
        ],
    )

    assert [slot["topic_direction"]["title"] for slot in slots] == [
        "郑州楼凤妹子怎么样",
        "主任最近约新妹子了",
        "郑州楼凤妹子怎么样",
    ]
    assert [slot["teacher_target"]["name"] for slot in slots] == [
        "花花老师身材服务真好",
        "新人榜单妹子",
        "花花老师身材服务真好",
    ]


def test_group_ai_deferred_items_preserve_slot_topic_and_teacher_targets():
    slots = [
        {
            "slot_id": "task-1:cycle:1:turn:1",
            "act_type": "short_react",
            "topic_direction": {"title": "郑州楼凤妹子怎么样"},
            "teacher_target": {"name": "花花老师"},
        }
    ]

    items = group_ai_chat._deferred_ai_planned_items(1, slots)

    assert items[0]["defer_ai_generation"] is True
    assert items[0]["slot"]["topic_direction"]["title"] == "郑州楼凤妹子怎么样"
    assert items[0]["slot"]["teacher_target"]["name"] == "花花老师"
    assert items[0]["slot_id"] == "task-1:cycle:1:turn:1"
    assert items[0]["act_type"] == "short_react"


def test_group_ai_hard_hourly_goal_10_defers_ai_generation():
    assert group_ai_chat._defer_ai_generation_for_plan({}, {"goal": 10}) is True


def test_group_ai_daily_coverage_without_reply_target_defers_ai_generation():
    config = {"account_coverage_mode": "all_accounts_daily"}

    assert group_ai_chat._defer_ai_generation_for_plan(config, {}, reply_target_count=0) is True
    assert group_ai_chat._defer_ai_generation_for_plan(config, {}, reply_target_count=1) is False


def test_group_ai_chat_normal_candidate_shortfall_keeps_partial_candidates(monkeypatch):
    task = SimpleNamespace(tenant_id=1, stats={})

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        assert count == 3
        return ["只返回一条普通发言"], 0

    monkeypatch.setattr(group_ai_chat, "generate_group_messages", fake_generate_group_messages)

    items, _tokens = group_ai_chat._generate_group_planned_items(
        None,
        task,
        {},
        reply_targets=[],
        normal_count=3,
        target_label="测试群",
        history="真人用户: 今天群里有什么安排",
    )

    assert [item["content"] for item in items] == ["只返回一条普通发言"]
    assert task.stats["normal_candidate_shortfall_count"] == 1


def test_group_ai_chat_empty_normal_candidate_shortfall_is_visible_failure(monkeypatch):
    task = SimpleNamespace(tenant_id=1, stats={})

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        assert count == 3
        return [], 0

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


def test_group_ai_quality_round_preserves_generated_material_metadata(monkeypatch):
    class ContentWithMetadata(str):
        def __new__(cls, value: str):
            obj = str.__new__(cls, value)
            obj.material_intent = "表情包:围观"
            obj.allow_material = True
            obj.intent = "附和"
            obj.mood = "轻松"
            return obj

    task = SimpleNamespace(tenant_id=1, stats={})

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        assert count == 1
        return [ContentWithMetadata("这个先蹲一下")], 3

    def fake_quality_filter(messages, *_args, **_kwargs):
        assert messages == ["这个先蹲一下"]
        return [{"content": "这个先蹲一下"}], {"ai_generation_candidate_count": 1}

    monkeypatch.setattr(group_ai_chat, "generate_group_messages", fake_generate_group_messages)
    monkeypatch.setattr(group_ai_chat, "_quality_filter_ai_messages", fake_quality_filter)

    quality_items, _tokens, _stats = group_ai_chat._generate_quality_filled_items(
        None,
        task,
        {},
        reply_targets=[],
        normal_count=1,
        target_label="测试群",
        history="真人A: 今天先看看",
        turn_count=1,
        duplicate_baseline_messages=[],
        chat_mode=group_ai_chat.CHAT_MODE_REPLY,
        context_message_ids=[1],
        fact_anchor_required=False,
        low_confidence_silence_enabled=False,
        fill_reply_shortfall_with_normal=False,
        enable_quality_fallback=False,
    )

    assert quality_items[0]["material_intent"] == "表情包:围观"
    assert quality_items[0]["allow_material"] is True
    assert quality_items[0]["intent"] == "附和"
    assert quality_items[0]["mood"] == "轻松"


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


def test_group_ai_pre_filter_drops_same_semantic_cluster_history():
    remaining = group_ai_chat._drop_repeated_ai_messages(
        ["照片没p", "价格问清楚了再说"],
        ["昨天照片准"],
    )

    assert remaining == ["价格问清楚了再说"]


def test_group_ai_prefilter_keeps_two_argument_helper_contract(monkeypatch):
    monkeypatch.setattr(group_ai_chat, "_drop_repeated_planned_items", lambda items, _previous: items)
    monkeypatch.setattr(
        group_ai_chat,
        "_quality_filter_ai_messages",
        lambda contents, _previous, **_kwargs: ([{"content": content} for content in contents], {}),
    )

    accepted = group_ai_chat._accept_quality_round(
        [],
        [{"content": "今晚照片准不准", "reply_target": None}],
        [],
        chat_mode=group_ai_chat.CHAT_MODE_REPLY,
        context_message_ids=[],
        fact_anchor_required=False,
        low_confidence_silence_enabled=False,
        remaining=1,
        stats={},
        rewrite_attempts=0,
    )

    assert accepted[0]["content"] == "今晚照片准不准"
    assert accepted[0]["rewrite_attempts"] == 0


def test_group_ai_prefilter_records_duplicate_rejection_stats(monkeypatch):
    monkeypatch.setattr(
        group_ai_chat,
        "_quality_filter_ai_messages",
        lambda contents, _previous, **_kwargs: ([{"content": content} for content in contents], {}),
    )
    stats: dict[str, object] = {}

    accepted = group_ai_chat._accept_quality_round(
        [],
        [{"content": "照片没p"}, {"content": "价格问清楚了再说"}],
        ["昨天照片准"],
        chat_mode=group_ai_chat.CHAT_MODE_REPLY,
        context_message_ids=[],
        fact_anchor_required=False,
        low_confidence_silence_enabled=False,
        remaining=2,
        stats=stats,
        rewrite_attempts=0,
    )

    assert [item["content"] for item in accepted] == ["价格问清楚了再说"]
    assert stats["duplicate_risk"] == "semantic_cluster"
    assert stats["skip_reason"] == "duplicate_risk"
    assert stats["quality_rejection_counts"]["duplicate_message"] == 1
    assert stats["quality_rejection_samples"][0]["content"] == "照片没p"


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
    assert all(item["quality_fallback"] in {"emoji_react", "safe_checkin"} for item in quality_items)
    assert [item["fallback_stage"] for item in quality_items] == ["static_safe_fallback", "static_safe_fallback"]
    assert [item["actual_model"] for item in quality_items] == ["static_safe_fallback", "static_safe_fallback"]
    assert all(item["fallback_reason"] == "all_model_stages_failed_or_rejected" for item in quality_items)
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


def test_group_ai_online_ready_clears_stale_offline_count(monkeypatch):
    task = SimpleNamespace(tenant_id=1, stats={"account_offline_count": 2, "other": "kept"})
    accounts = [SimpleNamespace(id=11), SimpleNamespace(id=12)]

    monkeypatch.setattr(
        group_ai_chat,
        "online_ready_account_ids_for_planning",
        lambda _session, *, tenant_id, accounts, now=None: {account.id for account in accounts},
    )

    ready = group_ai_chat._online_ready_accounts(None, task, accounts, {})

    assert ready == accounts
    assert task.stats == {"other": "kept"}


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
    captured: dict[str, object] = {}

    def fake_generate_contents(_session, _tenant_id, _config, bundle, **_kwargs):
        captured["bundle"] = bundle
        return ["停车位那句我懂"], 0

    monkeypatch.setattr(ai_generator, "_generate_group_prompt_contents", fake_generate_contents)

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

    user_prompt = captured["bundle"].user_prompt
    assert "身材" in user_prompt
    assert "服务反馈" not in user_prompt
    assert "装修预算" not in user_prompt


def test_group_ai_prompt_includes_fixed_generation_slots(monkeypatch):
    captured: dict[str, object] = {}

    def fake_generate_contents(_session, _tenant_id, _config, bundle, **_kwargs):
        captured["bundle"] = bundle
        return ["这句按一号短接", "这句按二号追问"], 0

    monkeypatch.setattr(ai_generator, "_generate_group_prompt_contents", fake_generate_contents)

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
                    "topic_direction": {"title": "郑州楼凤妹子怎么样"},
                    "teacher_target": {"name": "花花老师"},
                },
                {
                    "slot_id": "task-1:cycle:1:turn:2",
                    "sequence_index": 2,
                    "account_id": 12,
                    "act_type": "light_question",
                    "account_profile": "中年谨慎，常追问价格和位置",
                    "topic_direction": {"title": "主任最近约新妹子了"},
                    "teacher_target": {"name": "新人榜单妹子"},
                },
            ],
        },
        count=2,
        target_label="测试群",
        history="真人A: 昨天照片准",
    )

    payload = captured["bundle"].input_payload
    assert payload["generation_slots"] == [
        {"sequence_index": 1, "slot_id": "task-1:cycle:1:turn:1", "account_id": 11, "act_type": "short_react"},
        {"sequence_index": 2, "slot_id": "task-1:cycle:1:turn:2", "account_id": 12, "act_type": "light_question"},
    ]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "楼凤" not in serialized
    assert "价格和位置" not in serialized
    assert "身材" in serialized


def test_group_ai_reply_prompt_layers_target_profile_as_style_not_fact(monkeypatch):
    captured: dict[str, object] = {}

    def fake_generate_contents(_session, _tenant_id, _config, bundle, **_kwargs):
        captured["bundle"] = bundle
        return ["这句我接一下"], 0

    monkeypatch.setattr(ai_generator, "_generate_group_prompt_contents", fake_generate_contents)

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
        reply_targets=[{"author": "真人A", "preview": "这位成年老师高跟鞋好看", "source": "group"}],
        target_label="测试群",
        history="真人A: 这位成年老师高跟鞋好看",
    )

    payload = captured["bundle"].input_payload
    assert payload["reply_targets"] == [{"preview": "这位成年老师高跟鞋好看"}]
    assert payload["context_source"] == "safe_context"
    assert payload["generation_slots"] == [
        {"sequence_index": 1, "slot_id": "task-1:cycle:1:turn:1", "account_id": 11, "act_type": "context_reply"}
    ]
    assert "装修预算" not in captured["bundle"].user_prompt


def test_group_ai_chat_reads_tenant_profile_without_target_identity():
    source = Path(group_ai_chat.__file__).read_text()

    assert "tenant_learning_profile_preview(session, task.tenant_id, GROUP_CHAT_SCENE)" in source
    assert "learning_profile_preview(session, task.tenant_id, task.target_id" not in source
    assert "target_id, GROUP_CHAT_SCENE" not in source
