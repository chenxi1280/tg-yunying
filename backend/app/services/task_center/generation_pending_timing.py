"""Rebase only unstarted jobs when the effective send slot changes."""
from sqlalchemy import or_, select, true

from app.models import Action, AiProviderAttempt, GenerationJob, Task
from app.timezone import as_beijing
from app.services._common import _now
from .ai_generation_timing import GENERATION_LOOKAHEAD, generation_not_before, generation_send_time_expression


def refresh_pending_generation_timing(session, *, task_type, limit):
    now = _now()
    attempted = select(AiProviderAttempt.id).where(AiProviderAttempt.generation_job_id == GenerationJob.id).exists()
    due_action = select(Action.id).where(*_action_conditions(GenerationJob, now)).exists()
    jobs = session.scalars(select(GenerationJob).join(Task, Task.id == GenerationJob.task_id).where(
        GenerationJob.state == "pending", GenerationJob.generation_owner_id == "",
        GenerationJob.request_hash == "", GenerationJob.next_retry_at.is_(None), ~attempted,
        GenerationJob.task_lifecycle_epoch == Task.task_lifecycle_epoch,
        Task.status == "running", Task.deleted_at.is_(None),
        Task.type == task_type if task_type else true(),
        GenerationJob.generation_not_before_at > now,
        due_action,
    ).order_by(GenerationJob.created_at, GenerationJob.id).limit(limit).with_for_update(skip_locked=True, of=GenerationJob))
    for job in jobs:
        action = session.scalar(select(Action).where(*_action_conditions(job, now))
            .order_by(Action.created_at.desc()).limit(1))
        if action is None or (action.result or {}).get("ai_provider_call_started_at"):
            continue
        expected = generation_not_before(action)
        if job.generation_not_before_at is None or as_beijing(job.generation_not_before_at) != expected:
            job.generation_not_before_at = expected
            job.job_version = int(job.job_version or 1) + 1


def _action_conditions(job, now):
    return (Action.task_id == job.task_id, Action.tenant_id == job.tenant_id,
        Action.task_lifecycle_epoch == job.task_lifecycle_epoch,
        Action.status == "pending", Action.claim_owner == "", Action.lease_owner == "",
        generation_send_time_expression() <= now + GENERATION_LOOKAHEAD,
        or_(Action.payload["generation_job_id"].as_string() == job.id,
            (Action.obligation_type == job.obligation_type) & (Action.obligation_id == job.obligation_id)))
