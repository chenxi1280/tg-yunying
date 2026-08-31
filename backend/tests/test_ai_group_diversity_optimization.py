from __future__ import annotations

import pytest

from app.services.task_center.ai_group_prompt import (
    ADULT_SYSTEM_PROMPT,
    build_group_prompt,
)

pytestmark = pytest.mark.no_postgres


def test_adult_system_prompt_contains_diversity_and_rich_vocabulary():
    assert "核心口语与事实规则" in ADULT_SYSTEM_PROMPT
    assert "事实锚点铁律" in ADULT_SYSTEM_PROMPT
    assert "严禁在没有群聊上下文证据时凭空捏造个人到店经历" in ADULT_SYSTEM_PROMPT
    assert "禁止附和、求推荐或声称个人经历" in ADULT_SYSTEM_PROMPT
    assert "词汇丰富度与句式多样性" in ADULT_SYSTEM_PROMPT
    assert "严格控制字数：每条 8 到 20 个汉字，短促干脆。" in ADULT_SYSTEM_PROMPT
    assert "素颜" in ADULT_SYSTEM_PROMPT
    assert "隔音" in ADULT_SYSTEM_PROMPT
    assert "停车" in ADULT_SYSTEM_PROMPT
    assert "确实”、“卧槽真假”、“蹲一个”、“+1" not in ADULT_SYSTEM_PROMPT
    assert "禁止附和、求推荐" in ADULT_SYSTEM_PROMPT


def test_generic_warmup_uses_the_adult_no_context_contract():
    config = {
        "adult_prompt_enabled": True,
        "content_route": "adult_service",
    }

    bundle = build_group_prompt(config, target_label="测试群", history="", count=1)

    assert bundle.context_source == "generic_warmup"
    assert bundle.sanitized_context == ()
    assert "只能提出不指向具体人物、资源、地点或服务的开放问题" in bundle.system_prompt


def test_build_group_prompt_respects_planner_slots():
    config = {
        "adult_prompt_enabled": True,
        "content_route": "adult_service",
        "topic_directions": [
            {"title": "新老师照片与身材辨析", "description": "辨析修图与真实身材"},
        ],
        "generation_slots": [
            {"slot_id": "slot_1", "sequence_index": 1, "topic_direction": {"title": "新老师照片与身材辨析"}},
            {"slot_id": "slot_2", "sequence_index": 2, "topic_direction": {"title": "工兵出击战报"}},
        ],
    }
    bundle = build_group_prompt(config, target_label="测试群", history="老哥们聊聊", count=2)
    slots = bundle.input_payload.get("generation_slots") or []
    assert len(slots) == 2
    assert slots[0]["topic_direction"]["title"] == "新老师照片与身材辨析"
    assert slots[1]["topic_direction"]["title"] == "工兵出击战报"


def test_adult_slots_receive_stable_four_persona_rotation():
    config = {
        "adult_prompt_enabled": True,
        "generation_slots": [
            {"slot_id": f"slot_{account_id}", "account_id": account_id}
            for account_id in range(1, 5)
        ],
    }

    bundle = build_group_prompt(config, target_label="测试群", history="老哥们聊聊", count=4)

    expected = ["探路工兵", "挑剔老炮", "随性吃瓜", "本地地胆"]
    assert [slot["persona"] for slot in bundle.input_payload["generation_slots"]] == expected
    assert [draft["persona"] for draft in bundle.output_contract["drafts"]] == expected


def test_adult_persona_assignment_preserves_explicit_account_and_topic_bindings():
    config = {
        "adult_prompt_enabled": True,
        "account_personas": {"2": "已有账号人设"},
        "generation_slots": [
            {
                "slot_id": "slot_1",
                "account_id": 1,
                "persona": "显式槽位人设",
                "topic_direction": {"title": "照片辨析"},
            },
            {
                "slot_id": "slot_2",
                "account_id": 2,
                "topic_direction": {"title": "工兵战报"},
            },
        ],
    }

    bundle = build_group_prompt(config, target_label="测试群", history="老哥们聊聊", count=2)
    slots = bundle.input_payload["generation_slots"]

    assert [slot["persona"] for slot in slots] == ["显式槽位人设", "已有账号人设"]
    assert [slot["topic_direction"]["title"] for slot in slots] == ["照片辨析", "工兵战报"]


if __name__ == "__main__":
    test_adult_system_prompt_contains_diversity_and_rich_vocabulary()
    test_generic_warmup_uses_the_adult_no_context_contract()
    test_build_group_prompt_respects_planner_slots()
    test_adult_slots_receive_stable_four_persona_rotation()
    test_adult_persona_assignment_preserves_explicit_account_and_topic_bindings()
    print("ALL PRD COMPLIANT PROMPT TESTS PASSED SUCCESSFULLY!")
