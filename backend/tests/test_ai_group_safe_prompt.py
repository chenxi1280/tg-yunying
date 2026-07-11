from __future__ import annotations

import json

import pytest

from app.services.task_center.ai_group_prompt import (
    DRAFT_KEYS,
    build_group_prompt,
    sanitize_group_messages,
)


pytestmark = pytest.mark.no_postgres


def _config() -> dict:
    return {
        "account_personas": {"11": "普通成年群友"},
        "account_profiles": {"11": "自然直接；价格面付；只接安全话题"},
        "active_topic_direction": {"title": "成年人的穿搭讨论", "description": "价格可约"},
        "active_teacher_target": {"name": "这位成年老师", "description": "身材曲线很好看"},
        "generation_slots": [{"sequence_index": 1, "slot_id": "slot-1", "account_id": 11, "act_type": "short_react"}],
    }


def test_sanitizes_transaction_and_age_risk_but_keeps_adult_appearance():
    messages = sanitize_group_messages(
        [
            "这位成年老师身材曲线很好看，多少钱能安排",
            "这位成年老师腿又长又白",
            "黑丝和高跟鞋很搭",
            "这位老师好嫩像学生妹",
            "私聊我发定位",
        ]
    )

    assert messages == [
        "这位成年老师身材曲线很好看",
        "这位成年老师腿又长又白",
        "黑丝和高跟鞋很搭",
    ]


def test_keeps_normal_interest_group_context_without_topic_allowlist():
    messages = sanitize_group_messages([
        "今天聊聊摄影构图和光线",
        "周末徒步路线有人走过吗",
        "多少钱 私聊安排 酒店见",
    ])

    assert messages == ["今天聊聊摄影构图和光线", "周末徒步路线有人走过吗"]


def test_builds_english_instructions_with_sanitized_chinese_data():
    bundle = build_group_prompt(
        _config(),
        target_label="天津上牌资源群",
        history="真人用户: 这位成年老师气质挺撩人\n广告号: 价格便宜 私聊安排",
        count=1,
    )

    assert "Generate Chinese community replies" in bundle.system_prompt
    assert "Every referenced person is an adult" in bundle.system_prompt
    assert "one JSON object only" in bundle.system_prompt
    assert bundle.context_source == "safe_context"
    assert "气质挺撩人" in bundle.user_prompt
    assert "价格便宜" not in bundle.user_prompt
    assert "天津上牌资源群" not in bundle.user_prompt
    assert bundle.input_payload["group_label"].startswith("生产群-")


def test_generic_warmup_has_no_unsafe_dynamic_text():
    bundle = build_group_prompt(
        _config(),
        target_label="交易资源群",
        history="多少钱 私聊安排 酒店见",
        count=1,
    )

    assert bundle.context_source == "generic_warmup"
    assert bundle.sanitized_context == ()
    assert "For generic_warmup" in bundle.system_prompt
    assert "多少钱" not in bundle.user_prompt


def test_output_contract_has_exact_keys_for_each_requested_draft():
    bundle = build_group_prompt(_config(), target_label="普通交流群", history="今天有人签到吗", count=2)
    contract = bundle.output_contract

    assert set(contract) == {"decision", "context_source", "drafts"}
    assert len(contract["drafts"]) == 2
    assert all(set(draft) == DRAFT_KEYS for draft in contract["drafts"])
    json.dumps(contract, ensure_ascii=False)


def test_reply_target_is_sanitized_before_prompting():
    bundle = build_group_prompt(
        _config(),
        target_label="普通交流群",
        history="今天有人签到吗",
        count=1,
        reply_targets=[{"author": "@contact", "preview": "老师高跟鞋好看 多少钱", "source": "私聊"}],
    )

    serialized = json.dumps(bundle.input_payload, ensure_ascii=False)
    assert "高跟鞋好看" in serialized
    assert "多少钱" not in serialized
    assert "@contact" not in serialized
    assert "私聊" not in serialized
