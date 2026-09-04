from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Action, GenerationJob

from .ai_generation_state import cached_generation_result
from .payloads import SendMessagePayload
from .generation_recovery_scope import generation_task_filter


ActionResolver = Callable[[Session, GenerationJob], Action | None]


def reset_generation_job_for_cached_retry(session: Session, data: dict) -> None:
    job_id = str(data.get("generation_job_id") or "")
    job = session.get(GenerationJob, job_id) if job_id else None
    if job is None:
        return
    expected_owner = str(data.get("ai_generation_claim_owner") or "")
    version = int(job.job_version or 1)
    epoch = int(job.generation_lease_epoch or 0)
    changed = session.execute(update(GenerationJob).where(
        GenerationJob.id == job.id,
        GenerationJob.state == "generating",
        GenerationJob.generation_owner_id == expected_owner,
        GenerationJob.generation_lease_epoch == epoch,
        GenerationJob.job_version == version,
    ).values(
        state="pending",
        generation_stage="persist_retry",
        next_retry_at=None,
        generation_owner_id="",
        lease_expires_at=None,
        stage_version=int(job.stage_version or 1) + 1,
        job_version=version + 1,
    ).execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        raise RuntimeError("generation_persist_retry_job_claim_lost")
    session.expire(job)


def recover_cached_unknown_jobs(
    session: Session,
    limit: int,
    *,
    action_resolver: ActionResolver,
    task_type: str | None = None,
) -> int:
    jobs = list(session.scalars(select(GenerationJob).where(
        GenerationJob.state == "unknown",
        generation_task_filter(task_type),
    ).order_by(GenerationJob.created_at, GenerationJob.id).limit(limit)))
    recovered = 0
    for job in jobs:
        action = action_resolver(session, job)
        if action is None or not _has_same_attempt_cache(action, job):
            continue
        version = int(job.job_version or 1)
        changed = session.execute(update(GenerationJob).where(
            GenerationJob.id == job.id,
            GenerationJob.state == "unknown",
            GenerationJob.job_version == version,
            GenerationJob.generation_owner_id == "",
        ).values(
            state="pending",
            generation_stage="persist_retry",
            next_retry_at=None,
            job_version=version + 1,
        ).execution_options(synchronize_session=False)).rowcount
        recovered += int(changed == 1)
    return recovered


def _has_same_attempt_cache(action: Action, job: GenerationJob) -> bool:
    data = dict(action.payload or {})
    if str(data.get("generation_job_id") or "") != job.id:
        return False
    try:
        payload = SendMessagePayload.model_validate(data)
    except ValueError:
        return False
    return cached_generation_result(payload) is not None


__all__ = ["recover_cached_unknown_jobs", "reset_generation_job_for_cached_retry"]
