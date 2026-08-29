from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Action, GenerationJob

from .ai_content_runtime import recover_terminal_pre_gateway_window_slots


ActionResolver = Callable[[Session, GenerationJob], Action | None]


def recover_pending_generation_residue(
    session: Session,
    limit: int,
    *,
    action_resolver: ActionResolver,
) -> int:
    bounded_limit = max(1, int(limit))
    recovered = recover_terminal_pre_gateway_window_slots(session, bounded_limit)
    for job in _candidate_jobs(session, bounded_limit - recovered):
        action = action_resolver(session, job)
        if not _is_unowned_pending_residue(action, job):
            continue
        recovered += _recover_action(session, action, job)
    return recovered


def _candidate_jobs(session: Session, limit: int) -> tuple[GenerationJob, ...]:
    if limit <= 0:
        return ()
    residue_exists = select(Action.id).where(
        Action.tenant_id == GenerationJob.tenant_id,
        Action.task_id == GenerationJob.task_id,
        Action.task_lifecycle_epoch == GenerationJob.task_lifecycle_epoch,
        Action.obligation_type == GenerationJob.obligation_type,
        Action.obligation_id == GenerationJob.obligation_id,
        Action.status == "pending",
        Action.claim_owner == "",
        Action.lease_owner == "",
        Action.payload["ai_generation_status"].as_string() == "generating",
        Action.payload["ai_generation_claim_owner"].as_string() == "",
        Action.payload["ai_generation_claim_token"].as_string() == "",
        Action.payload["generation_job_id"].as_string() == GenerationJob.id,
    ).exists()
    return tuple(session.scalars(select(GenerationJob).where(
        GenerationJob.state == "pending",
        GenerationJob.generation_owner_id == "",
        residue_exists,
    ).order_by(GenerationJob.created_at, GenerationJob.id).limit(max(1, int(limit)))))


def _is_unowned_pending_residue(
    action: Action | None,
    job: GenerationJob,
) -> bool:
    if action is None or action.status != "pending":
        return False
    data = dict(action.payload or {})
    return bool(
        data.get("ai_generation_status") == "generating"
        and str(data.get("generation_job_id") or "") == job.id
        and not job.generation_owner_id
        and not action.claim_owner
        and not action.lease_owner
        and not str(data.get("ai_generation_claim_owner") or "")
        and not str(data.get("ai_generation_claim_token") or "")
        and not (action.result or {}).get("ai_provider_call_started_at")
    )


def _recover_action(session: Session, action: Action, job: GenerationJob) -> int:
    data = dict(action.payload or {})
    data["ai_generation_status"] = "pending"
    expected_version = int(action.action_version or 1)
    job_still_pending = select(GenerationJob.id).where(
        GenerationJob.id == job.id,
        GenerationJob.state == "pending",
        GenerationJob.generation_owner_id == "",
    ).exists()
    return int(session.execute(update(Action).where(
        Action.id == action.id,
        Action.status == "pending",
        Action.action_version == expected_version,
        Action.claim_owner == "",
        Action.lease_owner == "",
        Action.payload["ai_generation_status"].as_string() == "generating",
        Action.payload["ai_generation_claim_owner"].as_string() == "",
        Action.payload["ai_generation_claim_token"].as_string() == "",
        Action.payload["generation_job_id"].as_string() == job.id,
        job_still_pending,
    ).values(
        payload=data,
        action_version=expected_version + 1,
    ).execution_options(synchronize_session=False)).rowcount)


__all__ = ["recover_pending_generation_residue"]
