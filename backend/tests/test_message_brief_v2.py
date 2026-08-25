from __future__ import annotations

import pytest

from app.services.task_center.message_brief_v2 import (
    MessageBriefV2,
    V2BriefContract,
    build_v2_planner_prompt,
    parse_brief_v2_item,
    v2_candidate_failure,
    v2_realizer_system_prompt,
)


pytestmark = pytest.mark.no_postgres


def _contract(*, route: str = "adult_service", mode: str = "adult_service_sensory") -> V2BriefContract:
    versions = {
        "general": "general_v3",
        "adult_service_sensory": "adult_service_sensory_v2",
    }
    return V2BriefContract(
        task_direction_snapshot_hash="a" * 64,
        content_policy_hash="b" * 64,
        window_plan_hash="c" * 64,
        context_route=route,
        content_mode=mode,
        route_evidence_ids=("f1",),
        prompt_contract_version=versions.get(mode, f"{mode}_v1"),
        example_set_version="adult_human_anchors_v1",
        forbidden_claim_categories=("price_assertion", "location_assertion"),
        negative_phrases=("签到", "努力加油"),
    )


def _item(*, category: str = "sensory_question", speech_act: str = "question") -> dict:
    contract = _contract()
    return {
        "slot_id": "slot-1",
        "speech_act": speech_act,
        "stance": "curious",
        "length_band": "micro",
        "punctuation_profile": "question" if speech_act == "question" else "none",
        "anchor_ids": ["f1"],
        "brief_contract_version": "message_brief_v2",
        "task_direction_snapshot_hash": contract.task_direction_snapshot_hash,
        "content_policy_hash": contract.content_policy_hash,
        "window_plan_hash": contract.window_plan_hash,
        "context_route": contract.context_route,
        "content_mode": contract.content_mode,
        "route_evidence_ids": ["f1"],
        "claims": [{
            "category": category,
            "speech_act": speech_act,
            "evidence_ids": ["f1"],
        }],
        "forbidden_claim_categories": ["price_assertion", "location_assertion"],
        "prompt_contract_version": contract.prompt_contract_version,
        "example_set_version": contract.example_set_version,
    }


def test_v2_planner_prompt_exposes_semantic_schema_without_hash_echo() -> None:
    prompt = build_v2_planner_prompt(
        slot_infos=[{
            "slot_id": "slot-1",
            "reply_to_message_id": "7788",
            "reply_preview": "老师今晚能约吗",
            "context_route": "adult_service_inquiry",
            "content_mode": "adult_service_inquiry",
            "route_evidence_ids": ["f1"],
            "task_direction_snapshot_hash": "a" * 64,
            "content_policy_hash": "b" * 64,
            "window_plan_hash": "c" * 64,
        }],
        allowed_facts=[{"fact_id": "f1", "text": "老师今晚能约吗"}],
        recent_briefs=[{"speech_act": "question", "length_band": "micro"}],
    )

    assert '"briefs"' in prompt
    assert '"claims"' in prompt
    assert '"price_question"' in prompt
    assert '"reply_to_message_id":"7788"' in prompt
    assert '"recent_brief_shapes"' in prompt
    assert "a" * 64 not in prompt
    assert "task_direction_snapshot_hash" not in prompt


def test_adult_sensory_question_is_grounded_without_becoming_template() -> None:
    brief = parse_brief_v2_item(
        _item(),
        slot_id="slot-1",
        valid_fact_ids=("f1",),
        contract=_contract(),
    )
    assert isinstance(brief, MessageBriefV2)
    assert v2_candidate_failure("水多不？", brief) == ""
    prompt = v2_realizer_system_prompt(brief)
    assert "水多不？" in prompt
    assert "必须以问号结尾" in prompt
    assert "不得使用逗号" in prompt
    assert "正文只能从" not in prompt
    assert v2_candidate_failure("这水量行不行？", brief) == ""


def test_adult_sensory_reaction_allows_grounded_variation() -> None:
    brief = parse_brief_v2_item(
        _item(category="sensory_reaction", speech_act="reaction"),
        slot_id="slot-1",
        valid_fact_ids=("f1",),
        contract=_contract(),
    )

    assert brief is not None
    prompt = v2_realizer_system_prompt(brief)
    assert "不得含问号、逗号、顿号、分号或省略号" in prompt
    assert v2_candidate_failure("好润", brief) == ""
    assert v2_candidate_failure("看着挺润", brief) == ""
    assert v2_candidate_failure("这也太润了", brief) == ""


def test_v2_realizer_translates_pause_profile_into_surface_rule() -> None:
    item = _item(category="sensory_reaction", speech_act="reaction")
    item["punctuation_profile"] = "pause"
    brief = parse_brief_v2_item(
        item,
        slot_id="slot-1",
        valid_fact_ids=("f1",),
        contract=_contract(),
    )

    assert brief is not None
    prompt = v2_realizer_system_prompt(brief)
    assert "至少包含一个逗号、顿号、分号或省略号" in prompt
    assert "不得含问号" in prompt


def test_v2_rejects_wrong_object_exact_price_and_general_forced_adult() -> None:
    sensory = parse_brief_v2_item(
        _item(),
        slot_id="slot-1",
        valid_fact_ids=("f1",),
        contract=_contract(),
    )
    assert sensory is not None
    assert v2_candidate_failure("裙子好润", sensory) == "sensory_object_wrong"
    assert v2_candidate_failure("水润感", sensory) == "sensory_expression_vague"
    assert v2_candidate_failure("嘴唇软软的", sensory) == "adult_cutesy_tone"
    assert v2_candidate_failure("水灵灵的", sensory) == "adult_cutesy_tone"
    assert v2_candidate_failure("好心动", sensory) == "adult_cutesy_tone"
    assert v2_candidate_failure("看着好滑", sensory) == "sensory_intent_missing"
    assert v2_candidate_failure("300元？", sensory) == "unsupported_claim"
    assert v2_candidate_failure("9元？", sensory) == "unsupported_claim"
    assert v2_candidate_failure("九元？", sensory) == "unsupported_claim"
    assert v2_candidate_failure("先签到再说", sensory) == "negative_lexicon_match"

    general_contract = _contract(route="general", mode="general")
    general_item = _item(category="grounded_reaction", speech_act="reaction")
    general_item.update({
        "speech_act": "reaction",
        "punctuation_profile": "none",
        "context_route": "general",
        "content_mode": "general",
        "prompt_contract_version": "general_v3",
    })
    general = parse_brief_v2_item(
        general_item,
        slot_id="slot-1",
        valid_fact_ids=("f1",),
        contract=general_contract,
    )
    assert general is not None
    assert v2_candidate_failure("好润", general) == "general_forced_adult"
    assert v2_candidate_failure("这身材真性感", general) == "general_forced_adult"
    assert v2_candidate_failure("飞机杯怎么挑", general) == "general_forced_adult"


def test_v2_rejects_invented_experience_even_when_policy_list_is_incomplete() -> None:
    sensory = parse_brief_v2_item(
        _item(category="sensory_reaction", speech_act="reaction"),
        slot_id="slot-1",
        valid_fact_ids=("f1",),
        contract=_contract(),
    )

    assert sensory is not None
    assert v2_candidate_failure("刚体验完，确实好润", sensory) == "unsupported_claim"
    assert v2_candidate_failure("前天刚去过，水滋滋", sensory) == "unsupported_claim"


def test_inquiry_candidate_must_match_frozen_claim_category() -> None:
    contract = _contract(route="adult_service", mode="adult_service_inquiry")
    item = _item(category="price_question", speech_act="question")
    item.update({
        "content_mode": "adult_service_inquiry",
        "prompt_contract_version": "adult_service_inquiry_v1",
    })
    brief = parse_brief_v2_item(
        item,
        slot_id="slot-1",
        valid_fact_ids=("f1",),
        contract=contract,
    )

    assert brief is not None
    prompt = v2_realizer_system_prompt(brief)
    assert "只问价格" in prompt
    assert "不加称呼或寒暄" in prompt
    assert "必须以问号结尾" in prompt
    assert v2_candidate_failure("价格多少？", brief) == ""
    assert v2_candidate_failure("上海还能约吗？", brief) == "claim_category_mismatch"


def test_v2_rejects_parallel_claim_arrays_and_contract_drift() -> None:
    invalid = _item()
    invalid["claims"] = [{
        "category": "sensory_question",
        "speech_act": "question",
        "evidence_ids": ["f9"],
    }]
    assert parse_brief_v2_item(
        invalid,
        slot_id="slot-1",
        valid_fact_ids=("f1",),
        contract=_contract(),
    ) is None

    drift = _item()
    drift["content_policy_hash"] = "d" * 64
    assert parse_brief_v2_item(
        drift,
        slot_id="slot-1",
        valid_fact_ids=("f1",),
        contract=_contract(),
    ) is None


def test_v2_accepts_server_bound_contract_without_model_echo() -> None:
    item = _item()
    for key in (
        "brief_contract_version",
        "task_direction_snapshot_hash",
        "content_policy_hash",
        "window_plan_hash",
        "context_route",
        "content_mode",
        "route_evidence_ids",
        "forbidden_claim_categories",
        "prompt_contract_version",
        "example_set_version",
    ):
        item.pop(key, None)

    brief = parse_brief_v2_item(
        item,
        slot_id="slot-1",
        valid_fact_ids=("f1",),
        contract=_contract(),
    )

    assert brief is not None
    assert brief.content_mode == "adult_service_sensory"
    assert brief.task_direction_snapshot_hash == "a" * 64


@pytest.mark.parametrize(
    ("category", "outer_speech_act", "claim_speech_act", "punctuation"),
    (
        ("sensory_question", "reaction", "question", "none"),
        ("sensory_reaction", "reaction", "question", "none"),
        ("sensory_question", "question", "question", "none"),
    ),
)
def test_v2_rejects_claim_and_question_shape_mismatches(
    category: str,
    outer_speech_act: str,
    claim_speech_act: str,
    punctuation: str,
) -> None:
    item = _item(category=category, speech_act=outer_speech_act)
    item["claims"][0]["speech_act"] = claim_speech_act
    item["punctuation_profile"] = punctuation

    assert parse_brief_v2_item(
        item,
        slot_id="slot-1",
        valid_fact_ids=("f1",),
        contract=_contract(),
    ) is None


def test_v2_rejects_anchor_outside_route_evidence() -> None:
    item = _item()
    item["anchor_ids"] = ["f2"]

    assert parse_brief_v2_item(
        item,
        slot_id="slot-1",
        valid_fact_ids=("f1", "f2"),
        contract=_contract(),
    ) is None
