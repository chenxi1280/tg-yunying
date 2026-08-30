from __future__ import annotations

import hashlib
from typing import Protocol

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import Action, GenerationJob

from .ai_content_runtime import invalidate_job_pre_gateway_slot


class GenerationClaim(Protocol):
    action_id: str
    job_id: str
    owner: str
    job_version: int
    generation_lease_epoch: int


def finish_owned_job(
    session: Session,
    claim: GenerationClaim,
    *,
    job: GenerationJob | None,
    action: Action | None,
    state: str,
    generation_stage: str | None = None,
) -> None:
    if job is None:
        raise RuntimeError("parallel_generation_job_claim_lost")
    if _terminal_finish_already_persisted(
        session, claim, job=job, action=action, state=state,
    ):
        return
    if state == "ready":
        _require_ready_action(job, action)
    expected_job_version = int(job.job_version or 1)
    if expected_job_version < claim.job_version:
        raise RuntimeError("parallel_generation_job_claim_lost")
    values = _job_finish_values(
        job, action, state=state, generation_stage=generation_stage,
    )
    values["job_version"] = expected_job_version + 1
    changed = session.execute(update(GenerationJob).where(
        GenerationJob.id == claim.job_id,
        GenerationJob.state == "generating",
        GenerationJob.generation_owner_id == claim.owner,
        GenerationJob.job_version == expected_job_version,
        GenerationJob.generation_lease_epoch == claim.generation_lease_epoch,
    ).values(**values).execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        raise RuntimeError("parallel_generation_job_claim_lost")
    session.expire(job)
    session.refresh(job)
    if state in {"failed", "cancelled"}:
        invalidate_job_pre_gateway_slot(session, job)


def _terminal_finish_already_persisted(
    session: Session,
    claim: GenerationClaim,
    *,
    job: GenerationJob,
    action: Action | None,
    state: str,
) -> bool:
    action_states = {"failed": {"failed"}, "cancelled": {"skipped", "failed"}}
    valid_action_states = action_states.get(state)
    matches = bool(
        valid_action_states
        and job.state == state
        and not job.generation_owner_id
        and job.lease_expires_at is None
        and int(job.generation_lease_epoch or 0) == claim.generation_lease_epoch
        and int(job.job_version or 1) >= claim.job_version
        and _job_matches_action(job, action)
        and action.status in valid_action_states
    )
    if not matches:
        return False
    invalidate_job_pre_gateway_slot(session, job)
    return True


def _require_ready_action(job: GenerationJob, action: Action | None) -> None:
    if action is None:
        raise RuntimeError("parallel_generation_ready_action_invalid")
    payload = dict(action.payload or {})
    content = str(payload.get("message_text") or "").strip()
    candidate_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    candidate_ready = bool(
        content
        and payload.get("ai_generation_status") == "ready"
        and action.candidate_hash == candidate_hash
    )
    if not _job_matches_action(job, action) or not candidate_ready:
        raise RuntimeError("parallel_generation_ready_action_invalid")


def _job_matches_action(job: GenerationJob, action: Action | None) -> bool:
    payload = dict(action.payload or {}) if action is not None else {}
    return bool(
        action
        and action.tenant_id == job.tenant_id
        and action.task_id == job.task_id
        and int(action.task_lifecycle_epoch or 1) == int(job.task_lifecycle_epoch or 1)
        and action.obligation_type == job.obligation_type
        and action.obligation_id == job.obligation_id
        and str(payload.get("generation_job_id") or "") == job.id
    )


def _job_finish_values(job, action, *, state, generation_stage) -> dict:
    evidence = dict(job.evaluator_evidence or {})
    if action is not None:
        evidence["reviewer"] = dict(
            (action.result or {}).get("evaluator_evidence") or {}
        )
    values = {
        "state": state,
        "next_retry_at": None,
        "candidate_hash": str(action.candidate_hash or "") if action else "",
        "evaluator_evidence": evidence,
        "generation_owner_id": "",
        "lease_expires_at": None,
    }
    if generation_stage is not None:
        values["generation_stage"] = generation_stage
    return values


__all__ = ["finish_owned_job"]
