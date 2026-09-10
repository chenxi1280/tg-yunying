"""Count current work once; historical generation failure is not send failure."""
from collections import Counter

from sqlalchemy import and_, case, func, or_, select

from app.models import Action, FulfillmentObligationProjection, GenerationJob
from app.timezone import BEIJING_TZ
from .ai_message_evidence_query import verified_message_action_ids
from .datetime_compat import ensure_aware


def generation_outcome_diagnostics(session, task, *, since=None):
    rows = _current_work_query(task, since=since)
    confirmed = set(session.scalars(verified_message_action_ids(task, since=since)))
    outcomes, stages = Counter(), Counter()
    for row in session.execute(rows):
        outcomes[generation_outcome(row, confirmed=row.id in confirmed)] += 1
        if row.job_state is not None:
            stages[f"{row.job_state}:{row.job_stage}"] += 1
    return {
        "scope": {"task_lifecycle_epoch": int(task.task_lifecycle_epoch or 1),
            "since": since.isoformat() if since else None,
            "anchor_fields": ["action.created_at", "action.executed_at", "latest_job.created_at"],
            "unit": "current_obligation_or_unbound_action"},
        "outcome_counts": dict(sorted(outcomes.items())),
        "latest_job_state_stage_counts": dict(sorted(stages.items())),
        "work_count": sum(outcomes.values()),
    }


def generation_outcome(row, *, confirmed):
    if confirmed:
        return "remote_confirmed"
    terminal = _terminal_outcome(row)
    if terminal:
        return terminal
    if row.generation_status == "ready" and row.has_content:
        return "content_ready"
    if row.generation_status == "emergency_pending":
        return "emergency_pending"
    return {"unknown": "provider_unknown", "failed": "generation_failed"}.get(row.job_state, "generation_pending")


def _terminal_outcome(row):
    if row.status in {"unknown_after_send", "closed_unknown"}:
        return "telegram_unknown"
    if row.status == "skipped" and str(row.error_code or "").startswith("c2_"):
        return "admission_blocked"
    if row.status == "success":
        return "receipt_without_visible_fact"
    if row.status in {"failed", "skipped", "cancelled", "expired"}:
        return "terminal_failed"
    return ""


def _current_work_query(task, *, since=None):
    owner = func.coalesce(func.nullif(Action.obligation_id, ""), Action.id)
    active = select(FulfillmentObligationProjection.id).where(
        FulfillmentObligationProjection.tenant_id == task.tenant_id,
        FulfillmentObligationProjection.task_id == task.id,
        FulfillmentObligationProjection.obligation_type == Action.obligation_type,
        FulfillmentObligationProjection.obligation_id == Action.obligation_id,
        FulfillmentObligationProjection.active_action_id == Action.id,
    ).correlate(Action).exists()
    ranked = select(Action.id, func.row_number().over(
        partition_by=(Action.obligation_type, owner),
        order_by=(case((active, 1), else_=0).desc(), Action.materialization_version.desc(),
            Action.created_at.desc(), Action.id.desc()),
    ).label("rank")).where(
        Action.tenant_id == task.tenant_id, Action.task_id == task.id,
        Action.task_type == "group_ai_chat", Action.action_type == "send_message",
        Action.task_lifecycle_epoch == int(task.task_lifecycle_epoch or 1),
    ).subquery()
    current_ids = select(ranked.c.id).where(ranked.c.rank == 1)
    jobs = _latest_jobs(task)
    query = select(*_generation_columns(jobs)).outerjoin(jobs, and_(
        jobs.c.obligation_type == Action.obligation_type, jobs.c.obligation_id == Action.obligation_id,
        jobs.c.rank == 1,
    )).where(Action.id.in_(current_ids))
    if since is not None:
        anchor = ensure_aware(since)
        wall = anchor.astimezone(BEIJING_TZ).replace(tzinfo=None)
        query = query.where(or_(Action.created_at >= wall, Action.executed_at >= wall, jobs.c.created_at >= anchor))
    return query


def _generation_columns(jobs):
    open_work = Action.status.in_(("pending", "claiming", "executing", "retryable_failed"))
    return (
        Action.id, Action.status,
        case((open_work, Action.payload["ai_generation_status"].as_string())).label("generation_status"),
        case((open_work, func.length(func.trim(Action.payload["message_text"].as_string())) > 0)).label("has_content"),
        case((Action.status == "skipped", Action.result["error_code"].as_string())).label("error_code"),
        jobs.c.state.label("job_state"), jobs.c.generation_stage.label("job_stage"),
    )


def _latest_jobs(task):
    job = GenerationJob
    return select(job.obligation_type, job.obligation_id, job.state, job.generation_stage, job.created_at,
        func.row_number().over(partition_by=(job.obligation_type, job.obligation_id),
            order_by=(job.generation_sequence.desc(), job.created_at.desc(), job.id.desc())).label("rank"),
    ).where(job.tenant_id == task.tenant_id, job.task_id == task.id,
        job.task_lifecycle_epoch == int(task.task_lifecycle_epoch or 1)).subquery()
