from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.services.task_center.ai_content_job_binding import (
    AiContentJobBindingError,
    bind_group_generation_contracts,
    enrich_group_generation_slots,
)
from app.services.task_center.ai_context_information import general_topic_lines
from app.services.task_center.two_stage_generation import plan_message_briefs, realize_message_content
from test_ai_content_job_binding import _engine, _seed
from two_stage_generation_test_support import planner_factory, realizer_factory, reviewer_factory


pytestmark = pytest.mark.no_postgres
CONFIG = {"ai_content_route_v2_enabled": True, "engagement_contract_version": "unified_engagement_v1"}


def _topic_payload(job_id, title="周末运动"):
    return SimpleNamespace(generation_job_id=job_id, group_id=7, ai_generation_history="",
                           ai_generation_context_mode="topic_only", topic_direction={},
                           ai_generation_topic_direction={"title": title})


def test_topic_only_real_v2_binding_preserves_configured_source_and_empty_human_anchors():
    with Session(_engine()) as session:
        task, action, job = _seed(session, routes=["general"])
        payload = _topic_payload(job.id)
        action.payload = vars(payload)
        config = bind_group_generation_contracts(session, task, [(action, payload)], config=CONFIG)
        slots = enrich_group_generation_slots(config, [(action, payload)], [vars(payload)])
        frozen = job.evaluator_evidence["generation_contract"]
        assert frozen["context_mode"] == "topic_only"
        assert frozen["evidence_source"] == "configured_topic"
        assert frozen["configured_topic_snapshot"] == {"title": "周末运动"}
        assert frozen["allowed_facts"] == {"f1": "周末运动"}
        assert frozen["anchor_message_ids"] == []
        assert frozen["anchor_author_ids"] == []
        assert config["_ai_content_contracts"][job.id]["context_route"] == "general"
        assert general_topic_lines(slots) == ["群话题：周末运动"]
        assert payload.topic_direction == {}


def test_topic_only_configuration_does_not_invent_current_adult_evidence():
    with Session(_engine()) as session:
        task, action, job = _seed(session, routes=["general", "adult_service_sensory"])
        payload = _topic_payload(job.id, "老师今晚水多不")
        action.payload = vars(payload)
        with pytest.raises(AiContentJobBindingError, match="context_route_evidence_missing"):
            bind_group_generation_contracts(session, task, [(action, payload)], config=CONFIG)


def test_topic_only_still_requires_authorized_route():
    with Session(_engine()) as session:
        task, action, job = _seed(session, routes=["adult_service_sensory"])
        payload = _topic_payload(job.id)
        action.payload = vars(payload)
        with pytest.raises(AiContentJobBindingError, match="context_route_unproven"):
            bind_group_generation_contracts(session, task, [(action, payload)], config=CONFIG)


def test_topic_only_real_v2_planner_realizer_and_review_share_configuration_evidence():
    with Session(_engine()) as session:
        task, action, job = _seed(session, routes=["general"])
        payload = _topic_payload(job.id)
        action.payload = vars(payload)
        config = bind_group_generation_contracts(session, task, [(action, payload)], config=CONFIG)
        slots = enrich_group_generation_slots(config, [(action, payload)], [{**vars(payload), "slot_id": "s1"}])
        config = {**config, "generation_slots": slots}
        planner = planner_factory([[{
            "slot_id": "s1", "speech_act": "question", "stance": "curious", "length_band": "short",
            "punctuation_profile": "question", "anchor_ids": ["f1"],
            "claims": [{"category": "fact_question", "speech_act": "question", "evidence_ids": ["f1"]}],
        }]])
        plans, _ = plan_message_briefs(session, 1, config, history_lines=[], slots=slots, planner=planner)
        assert plans[0].rejection_code == ""
        realizer = realizer_factory([{
            "content": "周末运动更喜欢跑步还是骑车？", "used_anchor_ids": ["f1"], "speech_act": "question",
            "voice_profile_version": "style_contract_v3",
        }])
        content, meta, _ = realize_message_content(session, 1, config, plans[0], history_lines=[],
                                                  realizer=realizer, reviewer=reviewer_factory())
        assert content == "周末运动更喜欢跑步还是骑车？"
        assert "周末运动" in realizer.calls[0]["user_prompt"]
        assert meta["semantic_review"]["decision"] == "pass"


@pytest.mark.parametrize("invalid", [None, "topic", "content", "anchor", "current", "job"])
def test_topic_only_gateway_requires_frozen_candidate_and_current_identity(invalid):
    from app.models import GenerationJob
    from app.services.task_center.ai_group_emergency_contract import digest
    from app.services.task_center.ai_group_topic_binding import (
        freeze_topic_only_context, bind_topic_only_candidate, validate_topic_only_candidate)
    from app.services.task_center.payloads import SendMessagePayload
    from tests.ai_group_emergency_support import seed_emergency_batch

    with Session(_engine()) as session:
        task, actions, _, _ = seed_emergency_batch(session)
        action = actions[0]
        payload = SendMessagePayload.model_validate({**action.payload,
            "ai_generation_context_mode": "topic_only", "ai_generation_history": "",
            "context_message_ids": [], "anchor_message_ids": [], "context_snapshot_message_id": 0,
            "ai_generation_topic_direction": {"title": "周末运动"}, "message_text": "周末跑步吗？"})
        action.payload = payload.model_dump()
        job = session.get(GenerationJob, payload.generation_job_id)
        freeze_topic_only_context(task, action, job=job, payload=payload)
        action.candidate_hash = digest(payload.message_text)
        bind_topic_only_candidate(session, action, payload)
        if invalid == "topic":
            payload = payload.model_copy(update={"ai_generation_topic_direction": {"title": "其他主题"}})
        elif invalid == "content":
            payload = payload.model_copy(update={"message_text": "另一条内容"})
        elif invalid == "anchor":
            payload = payload.model_copy(update={"reply_to_message_id": 123})
        elif invalid == "current":
            action.payload = {**action.payload, "ai_generation_topic_direction": {"title": "已变更主题"}}
        elif invalid == "job":
            job.task_lifecycle_epoch += 1
        if invalid:
            with pytest.raises(ValueError, match="topic_only_"):
                validate_topic_only_candidate(session, action, payload)
        else:
            validate_topic_only_candidate(session, action, payload)
