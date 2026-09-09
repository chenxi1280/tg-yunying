from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.services.task_center.ai_generation_pipeline import generate_quality_results
from app.services.task_center.two_stage_generation import (
    TwoStageRealizeError,
    plan_message_briefs,
    realize_message_content,
)
from test_two_stage_pipeline_integration import (
    _sqlite_engine,
    _two_stage_dependencies,
    _two_stage_request,
)
from two_stage_generation_test_support import (
    brief_payload,
    planner_factory,
    realizer_factory,
)


pytestmark = pytest.mark.no_postgres


def _output(**overrides):
    return {
        "content": "今天先聊聊这个天气",
        "used_anchor_ids": ["f1"],
        "speech_act": "follow_up",
        "voice_profile_version": "style_contract_v3",
        **overrides,
    }


def _reject(output, *, config=None):
    plans, _ = plan_message_briefs(
        SimpleNamespace(), 1, {}, history_lines=["今天聊聊天气"],
        slots=[{"slot_id": "s1", "account_id": 0}],
        planner=planner_factory([[brief_payload("s1")]]),
    )

    def unexpected_review(*_args, **_kwargs):
        pytest.fail("parse rejection must not invoke the reviewer")

    with pytest.raises(TwoStageRealizeError) as caught:
        realize_message_content(
            SimpleNamespace(), 1, config or {}, plans[0],
            history_lines=["今天聊聊天气"], realizer=realizer_factory([output]),
            reviewer=unexpected_review,
        )
    return caught.value


@pytest.mark.parametrize(("output", "code"), [
    ([], "realizer_output_not_json_object"),
    (_output(content=""), "realizer_output_empty_content"),
    (_output(voice_profile_version=""), "realizer_voice_version_missing"),
    (_output(voice_profile_version="wrong"), "realizer_voice_version_mismatch"),
    (_output(used_anchor_ids=[]), "realizer_anchor_missing"),
    (_output(used_anchor_ids=["forged"]), "realizer_anchor_out_of_allowed"),
    (_output(speech_act="agreement"), "realizer_speech_act_mismatch"),
    (_output(content="天气不错"), "realizer_length_band_mismatch"),
    (_output(content="今天先聊聊这个天气？"), "realizer_punctuation_profile_mismatch"),
])
def test_parse_rejection_retains_consumed_tokens(output, code):
    error = _reject(output)

    assert error.code == code
    assert error.tokens == 5


def test_grounding_identity_rejection_retains_consumed_tokens():
    error = _reject(_output(), config={"_comment_grounding_assignment": {
        "assignment_id": "assignment-1", "primary_evidence_id": "e-1",
        "teacher_candidate_id": "teacher-1", "speech_act": "follow_up",
    }})

    assert error.code == "realizer_grounding_identity_mismatch"
    assert error.tokens == 5
    assert error.evidence == {}


def test_shape_evidence_uses_existing_normalization_without_copying_body():
    output = _output(content="  天气   好？  ", provider_secret="must-not-copy")
    before = dict(output)
    error = _reject(output)
    normalized = "天气 好？"

    assert error.evidence == {"realizer_shape": {
        "normalized_character_count": len(normalized),
        "expected_length_band": "short", "actual_length_band": "micro",
        "expected_punctuation_profile": "none",
        "actual_punctuation_profile": "question",
        "candidate_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }}
    serialized = json.dumps(error.evidence, ensure_ascii=False)
    assert normalized not in serialized
    assert "must-not-copy" not in serialized
    assert output == before


def test_quality_wait_totals_both_rejected_calls_and_preserves_last_evidence():
    planner = planner_factory([[brief_payload("slot-1")]])
    realizer = realizer_factory([_output(content="天气不错"), _output(content="天气晴朗")])
    engine = _sqlite_engine()
    with Session(engine) as session:
        results, tokens = generate_quality_results(
            session, _two_stage_request(), _two_stage_dependencies(planner, realizer),
        )
    engine.dispose()

    result = results[0]
    assert result.rejection_code == "quality_wait"
    assert str(result.content) == "[quality_wait:1]"
    assert tokens == 20  # Planner 10 + two rejected realizer calls of 5 each.
    assert len(realizer.calls) == 2
    assert result.evaluator_evidence["realizer_shape"]["candidate_sha256"] == (
        hashlib.sha256("天气晴朗".encode("utf-8")).hexdigest()
    )
    assert "normalized_character_count" not in realizer.calls[1]["user_prompt"]
