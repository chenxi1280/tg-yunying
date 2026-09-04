from dataclasses import replace
from datetime import timedelta

import pytest

from app.models import Action, ExecutionAttempt, Task
from app.services.task_center.engagement_timing_measurements import record_execution_timing_sample, safety_margin_ms
from app.services.task_center.engagement_timing_profiles import publish_execution_timing_profile
from tests.test_engagement_timing_profiles import _approval, _spec, session  # noqa: F401


pytestmark = pytest.mark.no_postgres


def _attempt(session):
    task = Task(id="timing-task", tenant_id=1, name="timing evidence QA", type="group_ai_chat")
    action = Action(id="timing-action", tenant_id=1, task_id=task.id, task_type=task.type,
        action_type="send_message", payload={})
    attempt = ExecutionAttempt(id="timing-attempt", tenant_id=1, action_id=action.id,
        gateway_call_started_at=_spec().boundaries["gateway_call_issued"])
    session.add(task)
    session.flush()
    session.add(action)
    session.flush()
    session.add(attempt)
    session.flush()
    return action, attempt


def test_remote_sample_matches_persisted_call_boundary(session):
    _, attempt = _attempt(session)
    sample = record_execution_timing_sample(session, replace(_spec(),
        evidence_kind="remote_attempt", evidence_reference=attempt.id))
    profile = publish_execution_timing_profile(session, _approval([sample]))
    assert sample.execution_attempt_id == attempt.id
    assert profile.confidence == "measured"


@pytest.mark.parametrize("field,value,reason", [
    ("tenant_id", 2, "scope_mismatch"),
    ("gateway_call_started_at", None, "endpoint_mismatch"),
])
def test_attempt_scope_and_call_boundary_are_not_inferred(session, *, field, value, reason):
    _, attempt = _attempt(session)
    setattr(attempt, field, value)
    with pytest.raises(ValueError, match=reason):
        record_execution_timing_sample(session, replace(_spec(),
            evidence_kind="remote_attempt", evidence_reference=attempt.id))


def test_wrong_adapter_cannot_reuse_attempt_evidence(session):
    action, attempt = _attempt(session)
    action.task_type = "channel_view"
    with pytest.raises(ValueError, match="scope_mismatch"):
        record_execution_timing_sample(session, replace(_spec(),
            evidence_kind="remote_attempt", evidence_reference=attempt.id))


def test_backwards_timestamps_and_invalid_hash_are_rejected(session):
    spec = _spec()
    with pytest.raises(ValueError, match="order_invalid"):
        record_execution_timing_sample(session, replace(spec,
            boundaries={**spec.boundaries, "ready_action": spec.boundaries["pre_provider"] - timedelta(seconds=1)}))
    with pytest.raises(ValueError, match="evidence_missing"):
        record_execution_timing_sample(session, replace(spec, evidence_hash="x" * 64))
    with pytest.raises(ValueError, match="sample_values_invalid"):
        safety_margin_ms(-1)
