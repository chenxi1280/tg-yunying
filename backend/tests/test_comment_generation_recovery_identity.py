import pytest
from sqlalchemy.orm import Session

from app.models import Action, GenerationJob, Task
from app.services._common import _now
from app.services.task_center.ai_generation_recovery import (
    _current_generation_action, reconcile_generation_jobs,
)
from tests.test_ai_generation_reconcile_fencing import (
    _action, _engine, _job, _seed_scope,
)


pytestmark = pytest.mark.no_postgres


def seed_comment(session, *, has_obligation=True, provider_started=False):
    _seed_scope(session)
    session.get(Task, "task-1").type = "channel_comment"
    identity = "comment-obligation" if has_obligation else "comment-action"
    job = _job("comment-job", identity, state="generating", owner="worker-old")
    job.obligation_type = "post_comment"
    action = _action("comment-action", identity, job.id, status="executing", owner="worker-old")
    action.task_type = "channel_comment"
    action.action_type = "post_comment"
    action.obligation_type = None
    action.obligation_id = None
    action.payload = {**action.payload,
        "comment_fulfillment_obligation_id": identity if has_obligation else ""}
    if provider_started:
        action.result = {"ai_provider_call_started_at": _now().isoformat()}
    session.add_all([job, action])
    session.commit()
    return job, action


@pytest.mark.parametrize("has_obligation", [True, False])
@pytest.mark.parametrize("provider_started", [True, False])
def test_comment_recovery_uses_original_generation_identity(has_obligation, provider_started):
    with Session(_engine()) as session:
        job, action = seed_comment(session, has_obligation=has_obligation,
                                   provider_started=provider_started)
        assert reconcile_generation_jobs(session, task_type="channel_comment") == 1
        session.commit()
        assert session.get(GenerationJob, job.id).state == ("unknown" if provider_started else "pending")
        current = session.get(Action, action.id)
        assert current.status == "pending"
        assert current.payload["ai_generation_status"] == (
            "ai_result_persist_unknown" if provider_started else "pending")
        assert current.claim_owner == ""
        assert reconcile_generation_jobs(session, task_type="channel_comment") == 0


@pytest.mark.parametrize("field,value", [
    ("tenant_id", 2), ("task_id", "other-task"), ("task_lifecycle_epoch", 2),
    ("task_type", "group_ai_chat"), ("action_type", "send_message"),
    ("comment_fulfillment_obligation_id", "other-obligation"),
    ("generation_job_id", "other-job"),
])
def test_comment_recovery_rejects_mismatched_identity(field, value):
    with Session(_engine()) as session:
        job, action = seed_comment(session)
        if field in {"comment_fulfillment_obligation_id", "generation_job_id"}:
            action.payload = {**action.payload, field: value}
        else:
            setattr(action, field, value)
        session.flush()
        assert _current_generation_action(session, job) is None
