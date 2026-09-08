from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import Action, Task
from app.services.task_center.ai_content_runtime import (
    bump_context_revision, claim_window_slot, freeze_window_plan, mark_candidate_ready,
)
from app.services.task_center.ai_generation_dispatch import _bind_ready_candidate_to_gateway
from app.services.task_center.ai_generator import AiGenerationUnavailable
from app.services.task_center.payloads import SendMessagePayload
from tests.test_ai_content_runtime_services import _engine, _job, _scope, _slot

pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 8, 19, 10)
CANDIDATE_HASH = "c" * 64


@pytest.fixture
def ready_candidate():
    engine = _engine()
    with Session(engine) as session:
        bump_context_revision(session, tenant_id=1, scope_type="group", scope_id="7", snapshot_hash="a" * 64)
        freeze_window_plan(session, _scope(NOW), (_slot(NOW, 1),))
        job = _job(NOW)
        session.add(job)
        session.flush()
        slot = claim_window_slot(session, job, lease_duration=timedelta(minutes=2))
        mark_candidate_ready(session, job, candidate_hash=CANDIDATE_HASH)
        task = Task(id="task-1", tenant_id=1, name="QA", type="group_ai_chat", config_revision=3)
        action = Action(id="QA", tenant_id=1, task_id=task.id, task_type=task.type,
            action_type="send_message", status="executing", candidate_hash=CANDIDATE_HASH)
        payload = SendMessagePayload(group_id=7, generation_job_id=job.id, message_text="今天挺凉快。",
            ai_generation_id="QA-generated", ai_generation_status="ready")
        bump_context_revision(session, tenant_id=1, scope_type="group", scope_id="7", snapshot_hash="b" * 64)
        yield session, task, action, job, slot, payload
    engine.dispose()


def test_normal_ready_content_keeps_original_identity_on_context_drift(ready_candidate):
    session, task, action, job, slot, payload = ready_candidate
    _bind_ready_candidate_to_gateway(session, task, action, payload)
    assert slot.state == "gateway_bound"
    assert job.candidate_hash == action.candidate_hash == CANDIDATE_HASH
    assert job.context_snapshot_version == 1
    assert job.evaluator_evidence["gateway_context_drift"] == {"frozen_revision": 1, "observed_revision": 2}


@pytest.mark.parametrize("changed", ({"reply_to_message_id": 77}, {"conversation_turn_claim_id": "turn-QA"}, {"interaction_opportunity_id": "opportunity-QA"}))
def test_reply_or_turn_bound_content_keeps_strict_context_check(ready_candidate, changed):
    session, task, action, job, slot, payload = ready_candidate
    with pytest.raises(AiGenerationUnavailable, match="context_stale"):
        _bind_ready_candidate_to_gateway(session, task, action, payload.model_copy(update=changed))
    assert slot.state == "invalidated"


def test_normal_content_still_rejects_changed_topic_policy(ready_candidate):
    session, task, action, job, slot, payload = ready_candidate
    job.evaluator_evidence = {"generation_contract": {"task_topic_revision": 2}}
    with pytest.raises(AiGenerationUnavailable, match="policy_stale"):
        _bind_ready_candidate_to_gateway(session, task, action, payload)
    assert slot.state == "invalidated"
