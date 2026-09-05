from datetime import timedelta

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Action, GenerationJob, Task, Tenant, TgAccount
from app.services._common import _now
from app.services.task_center.ai_generation_parallel import _candidate_statement


TENANT_ID = 991349
TASK_ID = "generation-fairness-pg"
ACCOUNT_ID = 991349
UNKNOWN_WINDOW_SIZE = 3


def test_postgres_unknown_prefix_does_not_starve_new_work():
    with SessionLocal() as session:
        _seed_candidates(session)
        candidates = list(session.scalars(_candidate_statement(1)))
        assert [action.id for action in candidates] == [f"{TASK_ID}-ready"]
        jobs = list(session.scalars(select(GenerationJob).where(
            GenerationJob.task_id == TASK_ID)))
        assert len(jobs) == UNKNOWN_WINDOW_SIZE
        assert all(job.state == "unknown" and job.job_version == 1 for job in jobs)
        session.rollback()


def _seed_candidates(session):
    session.add(Tenant(id=TENANT_ID, name="generation fairness postgres"))
    session.flush()
    session.add(TgAccount(id=ACCOUNT_ID, tenant_id=TENANT_ID,
                          display_name="candidate account", phone_masked="test"))
    session.add(Task(id=TASK_ID, tenant_id=TENANT_ID, name="candidate fairness",
                     type="group_ai_chat", status="running",
                     fulfillment_contract_version="fact_first_v3"))
    session.flush()
    for index in range(UNKNOWN_WINDOW_SIZE + 1):
        is_unknown = index < UNKNOWN_WINDOW_SIZE
        identity = f"{TASK_ID}-{index if is_unknown else 'ready'}"
        session.add(Action(id=identity, tenant_id=TENANT_ID, task_id=TASK_ID,
            task_type="group_ai_chat", action_type="send_message", account_id=ACCOUNT_ID,
            task_lifecycle_epoch=1, status="pending", obligation_type="coverage",
            obligation_id=identity, scheduled_at=_now() - timedelta(hours=1),
            payload={"ai_generation_status": "ai_result_persist_unknown" if is_unknown else "pending",
                     "message_text": ""}))
        if is_unknown:
            session.add(GenerationJob(tenant_id=TENANT_ID, task_id=TASK_ID,
                task_lifecycle_epoch=1, obligation_type="coverage", obligation_id=identity,
                state="unknown", generation_sequence=1, context_snapshot_version=1))
    session.flush()


@pytest.mark.parametrize("task_type", ["group_ai_chat", "channel_comment"])
def test_postgres_deferred_schedule_overrides_stale_effective_claim(task_type):
    from app.services.task_center.ai_generation_timing import generation_send_time_expression
    from app.services.task_center.comment_generation_worker import _comment_candidate_statement
    from app.timezone import as_beijing

    with SessionLocal() as session:
        _seed_candidates(session)
        task = session.get(Task, TASK_ID)
        task.type = task_type
        action = session.get(Action, f"{TASK_ID}-ready")
        action.task_type = task_type
        action.action_type = "post_comment" if task_type == "channel_comment" else "send_message"
        action.effective_claim_at = _now() - timedelta(minutes=30)
        deferred_until = _now() + timedelta(hours=2)
        action.scheduled_at = deferred_until
        action.release_not_before_at = deferred_until
        session.flush()
        query = _comment_candidate_statement(session) if task_type == "channel_comment" else _candidate_statement(1)
        assert list(session.scalars(query)) == []
        actual = session.scalar(select(generation_send_time_expression()).where(Action.id == action.id))
        assert as_beijing(actual) == deferred_until
        session.rollback()
