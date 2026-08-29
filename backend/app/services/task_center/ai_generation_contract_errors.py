from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import Action, GenerationJob
from app.services._common import _now

from .ai_content_runtime import invalidate_job_pre_gateway_slot
from .ai_generation_claim_lifecycle import owns_generation_claim
from .runtime_resources import _release_runtime_resources


@dataclass(frozen=True)
class GenerationContractErrorTarget:
    action_id: str
    owner: str
    token: str
    job_id: str = ""


def terminate_generation_contract_error(
    session_factory,
    target: GenerationContractErrorTarget,
    error: Exception,
) -> None:
    with session_factory() as session:
        action = session.get(Action, target.action_id)
        if not owns_generation_claim(action, target.owner, target.token):
            raise RuntimeError("generation_contract_error_action_claim_lost")
        job = _generation_job(session, action, target.job_id)
        if target.job_id and job is None:
            raise RuntimeError("generation_contract_error_job_missing")
        if job is not None:
            _fail_owned_job(session, job, owner=target.owner)
        _fail_owned_action(session, action, target=target, error=error)
        if job is not None:
            session.refresh(job)
            invalidate_job_pre_gateway_slot(session, job)
        _release_runtime_resources(action)
        session.commit()


def _generation_job(
    session: Session,
    action: Action,
    explicit_job_id: str,
) -> GenerationJob | None:
    payload = dict(action.payload or {})
    payload_job_id = str(payload.get("generation_job_id") or "")
    if explicit_job_id and payload_job_id != explicit_job_id:
        raise RuntimeError("generation_contract_error_job_identity_mismatch")
    job_id = explicit_job_id or payload_job_id
    job = session.get(GenerationJob, job_id) if job_id else None
    if job is not None and not _job_matches_action(job, action):
        raise RuntimeError("generation_contract_error_job_identity_mismatch")
    return job


def _job_matches_action(job: GenerationJob, action: Action) -> bool:
    return bool(
        job.tenant_id == action.tenant_id
        and job.task_id == action.task_id
        and int(job.task_lifecycle_epoch or 1) == int(action.task_lifecycle_epoch or 1)
        and job.obligation_type == action.obligation_type
        and job.obligation_id == action.obligation_id
    )


def _fail_owned_job(
    session: Session,
    job: GenerationJob,
    *,
    owner: str,
) -> None:
    expected_version = int(job.job_version or 1)
    expected_epoch = int(job.generation_lease_epoch or 0)
    changed = session.execute(update(GenerationJob).where(
        GenerationJob.id == job.id,
        GenerationJob.state == "generating",
        GenerationJob.generation_owner_id == owner,
        GenerationJob.job_version == expected_version,
        GenerationJob.generation_lease_epoch == expected_epoch,
    ).values(
        state="failed",
        generation_stage="contract_error",
        generation_owner_id="",
        lease_expires_at=None,
        next_retry_at=None,
        job_version=expected_version + 1,
    ).execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        raise RuntimeError("generation_contract_error_job_claim_lost")
    session.expire(job)


def _fail_owned_action(
    session: Session,
    action: Action,
    *,
    target: GenerationContractErrorTarget,
    error: Exception,
) -> None:
    payload = dict(action.payload or {})
    payload.update({
        "ai_generation_status": "failed",
        "ai_generation_claim_owner": "",
        "ai_generation_claim_token": "",
    })
    result = {
        **dict(action.result or {}),
        "success": False,
        "error_code": "generation_contract_error",
        "error_type": type(error).__name__[:120],
        "error_fingerprint": _error_fingerprint(error),
        "generation_outcome": "failed",
    }
    expected_version = int(action.action_version or 1)
    changed = session.execute(update(Action).where(
        Action.id == target.action_id,
        Action.status == "executing",
        Action.claim_owner == target.owner,
        Action.claim_token == target.token,
        Action.action_version == expected_version,
    ).values(
        payload=payload,
        result=result,
        status="failed",
        claim_owner="",
        claim_token="",
        claim_expires_at=None,
        lease_owner="",
        lease_expires_at=None,
        executed_at=_now(),
        action_version=expected_version + 1,
    ).execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        raise RuntimeError("generation_contract_error_action_claim_lost")


def _error_fingerprint(error: Exception) -> str:
    identity = f"{type(error).__module__}.{type(error).__qualname__}:{error}"
    return hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()


__all__ = ["GenerationContractErrorTarget", "terminate_generation_contract_error"]
