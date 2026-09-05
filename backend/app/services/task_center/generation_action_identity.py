"""Resolve each adapter's existing generation identity without changing it."""
from sqlalchemy import and_, func, select

from app.models import Action, GenerationJob


COMMENT_OBLIGATION_TYPE = "post_comment"


def current_generation_action(session, job: GenerationJob) -> Action | None:
    return session.scalar(select(Action).where(
        Action.tenant_id == job.tenant_id,
        Action.task_id == job.task_id,
        Action.task_lifecycle_epoch == job.task_lifecycle_epoch,
        Action.payload["generation_job_id"].as_string() == job.id,
        _obligation_condition(job),
    ).order_by(Action.action_version.desc(), Action.id.desc()).limit(1))


def _obligation_condition(job):
    if job.obligation_type == COMMENT_OBLIGATION_TYPE:
        identity = func.coalesce(func.nullif(
            Action.payload["comment_fulfillment_obligation_id"].as_string(), ""), Action.id)
        return and_(Action.task_type == "channel_comment",
                    Action.action_type == "post_comment", identity == job.obligation_id)
    return and_(Action.obligation_type == job.obligation_type,
                Action.obligation_id == job.obligation_id)


def job_matches_action(job: GenerationJob, action: Action) -> bool:
    if (job.tenant_id, job.task_id, int(job.task_lifecycle_epoch or 1)) != (
        action.tenant_id, action.task_id, int(action.task_lifecycle_epoch or 1)
    ):
        return False
    if action.task_type == "channel_comment":
        identity = str((action.payload or {}).get("comment_fulfillment_obligation_id") or action.id)
        return (action.action_type == "post_comment"
                and job.obligation_type == COMMENT_OBLIGATION_TYPE
                and job.obligation_id == identity)
    return (job.obligation_type == action.obligation_type
            and job.obligation_id == action.obligation_id)
