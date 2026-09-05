from copy import deepcopy

import pytest

from app.services.task_center.ai_generation_pipeline import _filter_slot, generate_quality_results
from app.services.task_center.ai_generator import GeneratedContent
from ai_generation_quality_test_support import _request

pytestmark = pytest.mark.no_postgres
GENERAL_TEXT = "明天的产品讨论从几点开始我好提前安排一下自己的时间"


def _scoped_request(*, v2=True, route="general"):
    config = {"ai_two_stage_enabled": True, "ai_content_route_v2_enabled": v2,
        "adult_prompt_enabled": True, "content_route": "adult_service_inquiry",
        "ai_content_allowed_routes": ["general", "adult_service_inquiry"],
        "generation_slots": [{"slot_id": "slot-1", "account_id": 11,
            "context_route": route, "content_mode": route}]}
    return _request(GENERAL_TEXT, config=config,
        history="真人用户: 明天安排产品讨论，开始时间未知")


def _filtered(request, *, baseline):
    return _filter_slot(request, 0, request.cached_contents[0], baseline=baseline)


def test_frozen_general_slot_does_not_inherit_task_adult_length():
    request = _scoped_request()
    original = deepcopy(request.config)
    result = _filtered(request, baseline=[])
    assert result.rejection_code == ""
    assert str(result.content) == GENERAL_TEXT
    assert request.config == original


@pytest.mark.parametrize("v2,route", [(False, "general"), (True, "adult_service_inquiry")])
def test_legacy_and_adult_slots_keep_existing_length_rule(v2, route):
    result = _filtered(_scoped_request(v2=v2, route=route), baseline=[])
    assert result.rejection_code == "adult_content_length_out_of_range"


def test_general_slot_keeps_duplicate_rejection():
    result = _filtered(_scoped_request(), baseline=[GENERAL_TEXT])
    assert result.rejection_code == "duplicate_risk"



def test_cached_mixed_slots_apply_their_own_frozen_route():
    request = _scoped_request()
    adult_slot = {**request.config["generation_slots"][0], "slot_id": "slot-2",
        "account_id": 12, "context_route": "adult_service_inquiry", "content_mode": "adult_service_inquiry"}
    request.config = {**request.config,
        "generation_slots": [request.config["generation_slots"][0], adult_slot]}
    request.batch_ids = ["action-1", "action-2"]
    request.cached_contents = [*request.cached_contents,
        GeneratedContent(GENERAL_TEXT, slot_id="slot-2", sequence_index=2)]
    request.quality_snapshots = [*request.quality_snapshots, request.quality_snapshots[0]]
    results, _ = generate_quality_results(None, request, None)
    assert [item.rejection_code for item in results] == ["", "adult_content_length_out_of_range"]
    assert [item.content.slot_id for item in results] == ["slot-1", "slot-2"]
