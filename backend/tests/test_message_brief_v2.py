from __future__ import annotations

import pytest

from app.services.task_center.message_brief_v2 import (
    MessageBriefV2,
    V2BriefContract,
    parse_brief_v2_item,
    v2_candidate_failure,
    v2_realizer_system_prompt,
)


pytestmark = pytest.mark.no_postgres


def _contract(*, route: str = "adult_service", mode: str = "adult_service_sensory") -> V2BriefContract:
    return V2BriefContract(
        task_direction_snapshot_hash="a" * 64,
        content_policy_hash="b" * 64,
        window_plan_hash="c" * 64,
        context_route=route,
        content_mode=mode,
        route_evidence_ids=("f1",),
        prompt_contract_version=f"{mode}_v1",
        example_set_version="adult_human_anchors_v1",
        forbidden_claim_categories=("price_assertion", "location_assertion"),
    )


def _item(*, category: str = "sensory_question", speech_act: str = "question") -> dict:
    contract = _contract()
    return {
        "slot_id": "slot-1",
        "speech_act": speech_act,
        "stance": "curious",
        "length_band": "micro",
        "punctuation_profile": "question",
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


def test_adult_sensory_question_is_grounded_without_becoming_template() -> None:
    brief = parse_brief_v2_item(
        _item(),
        slot_id="slot-1",
        valid_fact_ids=("f1",),
        contract=_contract(),
    )
    assert isinstance(brief, MessageBriefV2)
    assert v2_candidate_failure("水多不？", brief) == ""
    assert "2到6字" in v2_realizer_system_prompt(brief)


def test_v2_rejects_wrong_object_exact_price_and_general_forced_adult() -> None:
    sensory = parse_brief_v2_item(
        _item(),
        slot_id="slot-1",
        valid_fact_ids=("f1",),
        contract=_contract(),
    )
    assert sensory is not None
    assert v2_candidate_failure("裙子好润", sensory) == "sensory_object_wrong"
    assert v2_candidate_failure("300元？", sensory) == "unsupported_claim"
    assert v2_candidate_failure("9元？", sensory) == "unsupported_claim"
    assert v2_candidate_failure("九元？", sensory) == "unsupported_claim"

    general_contract = _contract(route="general", mode="general")
    general_item = _item(category="grounded_reaction", speech_act="reaction")
    general_item.update({
        "speech_act": "reaction",
        "punctuation_profile": "none",
        "context_route": "general",
        "content_mode": "general",
        "prompt_contract_version": "general_v1",
    })
    general = parse_brief_v2_item(
        general_item,
        slot_id="slot-1",
        valid_fact_ids=("f1",),
        contract=general_contract,
    )
    assert general is not None
    assert v2_candidate_failure("好润", general) == "general_forced_adult"


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
