from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.services.task_center.two_stage_generation import (
    TwoStageRealizeError,
    plan_message_briefs,
    realize_message_content,
    two_stage_enabled,
)
from two_stage_generation_test_support import (
    brief_payload as _brief_payload,
    planner_factory as _planner,
    realizer_factory as _realizer,
    reviewer_factory as _reviewer,
)


pytestmark = pytest.mark.no_postgres


def test_two_stage_enabled_reads_task_flag() -> None:
    assert two_stage_enabled({"ai_two_stage_enabled": True}) is True
    assert two_stage_enabled({"ai_two_stage_enabled": False}) is False
    assert two_stage_enabled({}) is False
    assert two_stage_enabled(None) is False


def test_plan_message_briefs_parses_valid_batch_with_single_call() -> None:
    planner = _planner([[
        _brief_payload("s1"),
        _brief_payload("s2", speech_act="agreement", stance="positive"),
    ]])

    plans, tokens = plan_message_briefs(
        SimpleNamespace(),
        1,
        {},
        history_lines=["今天聊聊天气"],
        slots=[{"slot_id": "s1", "account_id": 11}, {"slot_id": "s2", "account_id": 12}],
        planner=planner,
    )

    assert tokens == 10
    assert len(planner.calls) == 1
    assert all(plan.rejection_code == "" for plan in plans)
    assert plans[0].brief.speech_act == "follow_up"
    assert plans[0].brief.allowed_facts == ("f1",)


def test_plan_message_briefs_uses_general_topic_when_history_is_silent() -> None:
    planner = _planner([[_brief_payload("s1")]])

    plans, _tokens = plan_message_briefs(
        SimpleNamespace(),
        1,
        {},
        history_lines=[],
        slots=[{
            "slot_id": "s1",
            "account_id": 11,
            "content_mode": "general",
            "topic_direction": {"title": "夜宵吃点啥"},
        }],
        planner=planner,
    )

    assert plans[0].brief is not None
    assert plans[0].brief.allowed_facts == ("f1",)
    assert "夜宵吃点啥" in planner.calls[0]["user_prompt"]


def test_plan_message_briefs_replans_once_on_batch_collapse_then_typed_reject() -> None:
    collapsed = [_brief_payload(f"s{i}") for i in range(3)]
    planner = _planner([collapsed, collapsed])

    plans, tokens = plan_message_briefs(
        SimpleNamespace(),
        1,
        {},
        history_lines=["今天聊聊天气"],
        slots=[{"slot_id": f"s{i}", "account_id": 11} for i in range(3)],
        planner=planner,
    )

    assert len(planner.calls) == 2
    assert "recent_briefs" in planner.calls[1]["user_prompt"] or "follow_up" in planner.calls[1]["user_prompt"]
    assert all(plan.rejection_code == "batch_style_collapse" for plan in plans)
    assert all(plan.brief is None for plan in plans)
    assert tokens == 20


def test_plan_message_briefs_marks_schema_invalid_and_reply_mismatch() -> None:
    planner = _planner([[
        {"slot_id": "s1", "speech_act": "shout"},
        {**_brief_payload("s2"), "reply_to_message_id": "999"},
    ]])

    plans, _tokens = plan_message_briefs(
        SimpleNamespace(),
        1,
        {},
        history_lines=["今天聊聊天气"],
        slots=[
            {"slot_id": "s1", "account_id": 11},
            {"slot_id": "s2", "account_id": 12, "reply_to_message_id": "555"},
        ],
        planner=planner,
    )

    assert plans[0].rejection_code == "malformed_output"
    assert plans[1].rejection_code == "brief_reply_target_mismatch"


def test_plan_message_briefs_rejects_wrong_slot_identity() -> None:
    planner = _planner([[_brief_payload("wrong-slot")]])

    plans, _tokens = plan_message_briefs(
        SimpleNamespace(),
        1,
        {},
        history_lines=["今天聊聊天气"],
        slots=[{"slot_id": "s1", "account_id": 11}],
        planner=planner,
    )

    assert plans[0].rejection_code == "brief_slot_id_mismatch"


def test_v2_rejects_evidence_contract_before_provider_call() -> None:
    planner = _planner([[]])
    slot = {
        "slot_id": "s1",
        "account_id": 11,
        "task_direction_snapshot_hash": "a" * 64,
        "content_policy_hash": "b" * 64,
        "window_plan_hash": "c" * 64,
        "context_route": "adult_service_inquiry",
        "content_mode": "adult_service_inquiry",
        "route_evidence_ids": ["f9"],
        "prompt_contract_version": "adult_service_inquiry_v1",
        "example_set_version": "adult_human_anchors_v1",
    }

    plans, tokens = plan_message_briefs(
        SimpleNamespace(),
        1,
        {"ai_content_route_v2_enabled": True},
        history_lines=["老师今晚能约吗"],
        slots=[slot],
        planner=planner,
    )

    assert tokens == 0
    assert planner.calls == []
    assert plans[0].rejection_code == "brief_evidence_mismatch"


def test_v2_plans_adult_inquiry_from_semantic_only_provider_output() -> None:
    planner = _planner([[{
        "slot_id": "s1",
        "speech_act": "question",
        "stance": "curious",
        "length_band": "short",
        "punctuation_profile": "question",
        "anchor_ids": ["f1"],
        "reply_to_message_id": "",
        "claims": [{
            "category": "booking_question",
            "speech_act": "question",
            "evidence_ids": ["f1"],
        }],
    }]])
    slot = {
        "slot_id": "s1",
        "account_id": 11,
        "task_direction_snapshot_hash": "a" * 64,
        "content_policy_hash": "b" * 64,
        "window_plan_hash": "c" * 64,
        "context_route": "adult_service_inquiry",
        "content_mode": "adult_service_inquiry",
        "route_evidence_ids": ["f1"],
        "prompt_contract_version": "adult_service_inquiry_v1",
        "example_set_version": "adult_human_anchors_v1",
    }

    plans, tokens = plan_message_briefs(
        SimpleNamespace(),
        1,
        {"ai_content_route_v2_enabled": True},
        history_lines=["老师今晚能约吗"],
        slots=[slot],
        planner=planner,
    )

    assert tokens == 10
    assert plans[0].rejection_code == ""
    assert plans[0].brief.content_mode == "adult_service_inquiry"
    assert plans[0].brief.claims[0].category == "booking_question"


def test_realize_message_content_returns_content_and_passes_feedback() -> None:
    plans, _tokens = plan_message_briefs(
        SimpleNamespace(),
        1,
        {},
        history_lines=["今天聊聊天气"],
        slots=[{"slot_id": "s1", "account_id": 0}],
        planner=_planner([[_brief_payload("s1")]]),
    )
    realizer = _realizer([{
        "content": "那天气后面具体怎么说",
        "used_anchor_ids": ["f1"],
        "speech_act": "follow_up",
        "voice_profile_version": "style_contract_v3",
    }])

    content, meta, tokens = realize_message_content(
        SimpleNamespace(),
        1,
        {},
        plans[0],
        history_lines=["今天聊聊天气"],
        rejection_feedback="template_shell_limited",
        realizer=realizer,
        reviewer=_reviewer(),
    )

    assert content == "那天气后面具体怎么说"
    assert meta["used_anchor_ids"] == ["f1"]
    assert meta["semantic_review"]["decision"] == "pass"
    assert tokens == 8
    assert "template_shell_limited" in realizer.calls[0]["user_prompt"]
    assert "今天聊聊天气" in realizer.calls[0]["user_prompt"]


def test_realize_message_content_raises_typed_structural_errors() -> None:
    plans, _tokens = plan_message_briefs(
        SimpleNamespace(),
        1,
        {},
        history_lines=["今天聊聊天气"],
        slots=[{"slot_id": "s1", "account_id": 0}],
        planner=_planner([[_brief_payload("s1")]]),
    )
    realizer = _realizer([
        {"content": "", "used_anchor_ids": ["f1"]},
        {"content": "ok", "voice_profile_version": "wrong"},
    ])

    with pytest.raises(TwoStageRealizeError, match="empty_content"):
        realize_message_content(
            SimpleNamespace(), 1, {}, plans[0], history_lines=[],
            realizer=realizer, reviewer=_reviewer(),
        )
    with pytest.raises(TwoStageRealizeError, match="voice_version_mismatch"):
        realize_message_content(
            SimpleNamespace(), 1, {}, plans[0], history_lines=[],
            realizer=realizer, reviewer=_reviewer(),
        )


def test_realize_message_content_rejects_semantic_reviewer_context_failure() -> None:
    plans, _tokens = plan_message_briefs(
        SimpleNamespace(),
        1,
        {},
        history_lines=["今天聊聊天气"],
        slots=[{"slot_id": "s1", "account_id": 0}],
        planner=_planner([[_brief_payload("s1")]]),
    )
    realizer = _realizer([{
        "content": "周五那个活动已经全部结束了",
        "used_anchor_ids": ["f1"],
        "speech_act": "follow_up",
        "voice_profile_version": "style_contract_v3",
    }])

    with pytest.raises(TwoStageRealizeError, match="context_mismatch") as exc_info:
        realize_message_content(
            SimpleNamespace(),
            1,
            {},
            plans[0],
            history_lines=["今天聊聊天气"],
            realizer=realizer,
            reviewer=_reviewer("fail", code="context_mismatch"),
        )

    assert exc_info.value.tokens == 8
    assert exc_info.value.evidence["decision"] == "fail"
    assert exc_info.value.evidence["codes"] == ["context_mismatch"]
    assert len(exc_info.value.evidence["candidate_hash"]) == 64


def test_realize_message_content_rejects_deterministic_unsupported_claim_before_review() -> None:
    plans, _tokens = plan_message_briefs(
        SimpleNamespace(),
        1,
        {},
        history_lines=["这个产品今天更新了"],
        slots=[{"slot_id": "s1", "account_id": 0}],
        planner=_planner([[_brief_payload("s1", forbidden_claims=["experience"])]]),
    )
    realizer = _realizer([{
        "content": "这个我买过感觉还行",
        "used_anchor_ids": ["f1"],
        "speech_act": "follow_up",
        "voice_profile_version": "style_contract_v3",
    }])
    reviewer_calls: list[str] = []

    def reviewer(*_args, **_kwargs):
        reviewer_calls.append("called")
        return _reviewer()(*_args, **_kwargs)

    with pytest.raises(TwoStageRealizeError, match="unsupported_claim") as exc_info:
        realize_message_content(
            SimpleNamespace(), 1, {}, plans[0],
            history_lines=["这个产品今天更新了"],
            realizer=realizer, reviewer=reviewer,
        )

    assert reviewer_calls == []
    assert exc_info.value.tokens == 5
    assert exc_info.value.evidence["lexical_grounding"]["unsupported_claim_marker"] == "experience"


def test_realize_message_content_rejects_pass_with_failure_codes() -> None:
    plans, _tokens = plan_message_briefs(
        SimpleNamespace(), 1, {}, history_lines=["今天聊聊天气"],
        slots=[{"slot_id": "s1", "account_id": 0}],
        planner=_planner([[_brief_payload("s1")]]),
    )
    realizer = _realizer([{
        "content": "今天继续聊聊这个天气",
        "used_anchor_ids": ["f1"],
        "speech_act": "follow_up",
        "voice_profile_version": "style_contract_v3",
    }])

    with pytest.raises(TwoStageRealizeError, match="unsupported_claim"):
        realize_message_content(
            SimpleNamespace(), 1, {}, plans[0],
            history_lines=["今天聊聊天气"],
            realizer=realizer,
            reviewer=_reviewer("pass", code="unsupported_claim"),
        )


def test_realize_message_content_requires_independent_reviewer_configuration() -> None:
    plans, _tokens = plan_message_briefs(
        SimpleNamespace(),
        1,
        {},
        history_lines=["今天聊聊天气"],
        slots=[{"slot_id": "s1", "account_id": 0}],
        planner=_planner([[_brief_payload("s1")]]),
    )
    realizer = _realizer([{
        "content": "今天继续聊聊这个天气",
        "used_anchor_ids": ["f1"],
        "speech_act": "follow_up",
        "voice_profile_version": "style_contract_v3",
    }])

    with pytest.raises(TwoStageRealizeError, match="semantic_reviewer_model_missing"):
        realize_message_content(
            SimpleNamespace(), 1, {}, plans[0],
            history_lines=["今天聊聊天气"], realizer=realizer,
        )

    with pytest.raises(TwoStageRealizeError, match="semantic_generator_model_missing"):
        realize_message_content(
            SimpleNamespace(), 1, {"ai_semantic_reviewer_model": "reviewer-v1"}, plans[0],
            history_lines=["今天聊聊天气"], realizer=realizer,
        )

    with pytest.raises(TwoStageRealizeError, match="semantic_reviewer_must_differ_from_generator"):
        realize_message_content(
            SimpleNamespace(), 1,
            {"ai_model": "mimo v2.5", "ai_semantic_reviewer_model": "xiaomi mimo-v2.5"},
            plans[0], history_lines=["今天聊聊天气"], realizer=realizer,
        )

    with pytest.raises(TwoStageRealizeError, match="semantic_reviewer_must_differ_from_generator"):
        realize_message_content(
            SimpleNamespace(), 1,
            {"ai_model": "custom  reviewer", "ai_semantic_reviewer_model": "CUSTOM REVIEWER"},
            plans[0], history_lines=["今天聊聊天气"], realizer=realizer,
        )
