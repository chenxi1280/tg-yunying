from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.task_center.message_brief import (
    BATCH_FINGERPRINT_LIMIT,
    MessageBrief,
    batch_style_collapse_reason,
    build_brief_planner_user_prompt,
    build_realizer_user_prompt,
    fact_id_map,
    length_band_of,
    opening_function_pattern,
    parse_brief_item,
    parse_realizer_response,
    punctuation_profile_of,
    structural_duplicate_indexes,
    structural_fingerprint,
    syntax_shape_of,
    voice_contract_v3,
)


pytestmark = pytest.mark.no_postgres


def _brief(**overrides) -> MessageBrief:
    values = {
        "slot_id": "slot-1",
        "speech_act": "question",
        "stance": "curious",
        "length_band": "short",
        "punctuation_profile": "question",
        "anchor_ids": ("f1",),
        "allowed_facts": ("f1", "f2"),
    }
    values.update(overrides)
    return MessageBrief(**values)


def test_parse_brief_item_accepts_valid_brief() -> None:
    item = {
        "slot_id": "slot-1",
        "speech_act": "follow_up",
        "stance": "neutral",
        "length_band": "micro",
        "punctuation_profile": "none",
        "anchor_ids": ["f1"],
    }

    brief = parse_brief_item(item, slot_id="slot-1", valid_fact_ids=("f1", "f2"))

    assert brief is not None
    assert brief.speech_act == "follow_up"
    assert brief.allowed_facts == ("f1", "f2")


def test_parse_brief_item_rejects_invalid_enum_and_missing_anchor() -> None:
    valid = {
        "speech_act": "question",
        "stance": "curious",
        "length_band": "short",
        "punctuation_profile": "question",
        "anchor_ids": ["f1"],
    }
    assert parse_brief_item({**valid, "speech_act": "shout"}, slot_id="s") is None
    assert parse_brief_item({**valid, "stance": "angry"}, slot_id="s") is None
    assert parse_brief_item({**valid, "length_band": "long"}, slot_id="s") is None
    assert parse_brief_item({**valid, "punctuation_profile": "exclaim"}, slot_id="s") is None
    no_anchor = {**valid, "anchor_ids": []}
    assert parse_brief_item(no_anchor, slot_id="s") is None
    outside = {**valid, "anchor_ids": ["f9"]}
    assert parse_brief_item(outside, slot_id="s", valid_fact_ids=("f1",)) is None


def test_parse_brief_item_allows_silence_without_anchor() -> None:
    item = {
        "speech_act": "silence",
        "stance": "neutral",
        "length_band": "micro",
        "punctuation_profile": "none",
    }

    brief = parse_brief_item(item, slot_id="slot-1")

    assert brief is not None
    assert brief.speech_act == "silence"
    assert brief.anchor_ids == ()


def test_batch_style_collapse_only_when_all_identical_and_large_enough() -> None:
    same = [_brief(slot_id=f"s{i}") for i in range(3)]
    assert batch_style_collapse_reason(same) == "batch_style_collapse"

    two = [_brief(slot_id="s1"), _brief(slot_id="s2")]
    assert batch_style_collapse_reason(two) == ""

    diverse = [
        _brief(slot_id="s1"),
        _brief(slot_id="s2", speech_act="agreement", length_band="micro", punctuation_profile="none"),
        _brief(slot_id="s3", speech_act="reaction"),
    ]
    assert batch_style_collapse_reason(diverse) == ""


def test_structural_dimensions_classify_content_shape() -> None:
    assert length_band_of("在吗") == "micro"
    assert length_band_of("这个群今天聊什么呢") == "short"
    assert length_band_of("这句话故意写得长一点用来落进 medium 档位里") == "medium"

    assert opening_function_pattern("怎么突然聊这个") == "interrogative_open"
    assert opening_function_pattern("哈哈这都能碰到") == "particle_open"
    assert opening_function_pattern("👍 稳") == "reaction_open"
    assert opening_function_pattern("确实有点意思") == "statement_open"

    assert punctuation_profile_of("这个多少钱？") == "question"
    assert punctuation_profile_of("还行，先看看") == "pause"
    assert punctuation_profile_of("稳") == "none"

    assert syntax_shape_of("就一句") == "single_clause"
    assert syntax_shape_of("先看一档，再说下一档") == "multi_clause"
    assert syntax_shape_of("这样行吗") == "single_clause+particle_tail"


def test_structural_duplicate_indexes_allows_two_public_reactions() -> None:
    fingerprints = ["a", "a", "a", "b", "b", "b"]

    duplicates = structural_duplicate_indexes(fingerprints)

    assert duplicates == {2, 5}
    assert BATCH_FINGERPRINT_LIMIT == 2


def test_structural_fingerprint_composes_all_dimensions() -> None:
    brief = _brief()

    fingerprint = structural_fingerprint(brief, "多少钱呢？")

    assert fingerprint == "question|micro|interrogative_open|question|single_clause+particle_tail"
    assert structural_fingerprint(None, "多少钱呢？").startswith("unplanned|")


def test_voice_contract_v3_derives_verifiable_dimensions() -> None:
    mask = SimpleNamespace(
        mask_name="老群友",
        sentence_length="偏好短句",
        interaction_habits=["爱提问", "常追问细节"],
        emoji_policy="少表情",
        lexical_preferences=["口语语气词多", "说“稳”"],
        tone_strength="直接、果断",
        preference_tags=["幽默", "热情"],
        forbidden_expressions=["绝对化承诺"],
        short_prompt_summary="短句直接，爱问细节",
    )

    voice = voice_contract_v3(mask)

    assert voice["voice_profile_version"] == "style_contract_v3"
    assert voice["length_mix"]["micro"] == 0.4
    assert voice["question_rate"] == "high"
    assert voice["emoji_rate"] == "rare"
    assert voice["sentence_final_particle_rate"] == "often"
    assert voice["assertiveness"] == "high"
    assert voice["humor_level"] == "high"
    assert voice["warmth"] == "high"
    assert voice["forbidden_patterns"] == ["绝对化承诺"]


def test_voice_contract_v3_defaults_without_mask() -> None:
    voice = voice_contract_v3(None)

    assert voice["question_rate"] == "medium"
    assert voice["emoji_rate"] == "occasional"
    assert voice["assertiveness"] == "medium"
    assert voice["summary"] == ""


def test_parse_realizer_response_validates_contract() -> None:
    brief = _brief()
    item = {
        "content": "这个价格现在是多少呢？",
        "used_anchor_ids": ["f1"],
        "speech_act": "question",
        "voice_profile_version": brief.voice_profile_version,
    }

    content, meta = parse_realizer_response(item, brief)
    assert content == "这个价格现在是多少呢？"
    assert meta["used_anchor_ids"] == ["f1"]

    with pytest.raises(ValueError, match="empty_content"):
        parse_realizer_response({**item, "content": " "}, brief)
    with pytest.raises(ValueError, match="voice_version_mismatch"):
        parse_realizer_response({**item, "voice_profile_version": "other"}, brief)
    with pytest.raises(ValueError, match="anchor_out_of_allowed"):
        parse_realizer_response({**item, "used_anchor_ids": ["f9"]}, brief)
    with pytest.raises(ValueError, match="not_json_object"):
        parse_realizer_response(["not-a-dict"], brief)


def test_parse_realizer_response_rejects_missing_grounding_and_contract_drift() -> None:
    brief = _brief()
    valid = {
        "content": "这个价格现在是多少呢？",
        "used_anchor_ids": ["f1"],
        "speech_act": "question",
        "voice_profile_version": brief.voice_profile_version,
    }

    with pytest.raises(ValueError, match="anchor_missing"):
        parse_realizer_response({**valid, "used_anchor_ids": []}, brief)
    with pytest.raises(ValueError, match="speech_act_mismatch"):
        parse_realizer_response({**valid, "speech_act": "agreement"}, brief)
    with pytest.raises(ValueError, match="voice_version_missing"):
        parse_realizer_response({**valid, "voice_profile_version": ""}, brief)
    with pytest.raises(ValueError, match="length_band_mismatch"):
        parse_realizer_response({**valid, "content": "短吗？"}, brief)
    with pytest.raises(ValueError, match="punctuation_profile_mismatch"):
        parse_realizer_response({**valid, "content": "这个价格现在是多少呢"}, brief)


def test_fact_id_map_sanitizes_forbidden_clauses() -> None:
    mapping = fact_id_map(["今天聊聊天气", "加我微信聊价格"])

    assert list(mapping) == ["f1"]
    assert mapping["f1"] == "今天聊聊天气"


def test_realizer_prompt_carries_feedback_and_anchor_texts() -> None:
    brief = _brief()

    prompt = build_realizer_user_prompt(
        brief,
        voice_contract_v3(None),
        anchor_texts={"f1": "今天聊聊天气", "f2": "群里签到"},
        reply_preview="昨天说的那个",
        rejection_feedback="template_shell_limited:空泛模板",
    )

    assert "今天聊聊天气" in prompt
    assert "群里签到" not in prompt
    assert "template_shell_limited" in prompt
    assert "昨天说的那个" in prompt


def test_brief_planner_prompt_lists_all_slots_and_recent_briefs() -> None:
    prompt = build_brief_planner_user_prompt(
        slot_infos=[{"slot_id": "s1", "account_id": 1, "reply_to_message_id": ""}],
        allowed_facts=[{"fact_id": "f1", "text": "今天聊聊天气"}],
        recent_briefs=[{"slot_id": "s0", "speech_act": "question", "length_band": "short"}],
    )

    assert "s1" in prompt
    assert "f1" in prompt
    assert "question" in prompt
