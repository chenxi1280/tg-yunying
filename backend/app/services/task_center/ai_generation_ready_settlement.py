"""Publish the original prepared Action and GenerationJob atomically."""
import hashlib
import json

from sqlalchemy import select

from app.models import Action, AiContentWindowPlanSlot, GenerationJob, Task

from .ai_generation_claim_lifecycle import owns_generation_claim, release_generation_claim
from .ai_generation_job_finish import finish_owned_job, require_ready_generation_action


def settle_prepared_generation(session_factory, claim) -> int:
    with session_factory() as session:
        initial = session.get(GenerationJob, claim.job_id)
        if initial is None:
            raise RuntimeError("parallel_generation_job_claim_lost")
        task = lock_generation_task(session, initial)
        action = session.scalar(select(Action).where(Action.id == claim.action_id)
            .with_for_update(nowait=True).execution_options(populate_existing=True))
        job = session.scalar(select(GenerationJob).where(GenerationJob.id == claim.job_id)
            .with_for_update(nowait=True).execution_options(populate_existing=True))
        if job is None or (job.tenant_id, job.task_id, job.task_lifecycle_epoch) != (
                task.tenant_id, task.id, task.task_lifecycle_epoch):
            raise RuntimeError("parallel_generation_job_claim_lost")
        require_ready_generation_action(job, action)
        receipt = _settlement_receipt(claim, action)
        if _settlement_already_committed(job, receipt):
            return 0
        released = _require_original_action_claim(action, claim)
        require_prepared_window(session, job, action)
        finish_owned_job(session, claim, job=job, action=action, state="ready")
        if released:
            release_generation_claim(action, dict(action.payload or {}))
        job.evaluator_evidence = {**dict(job.evaluator_evidence or {}),
                                 "generation_settlement": receipt}
        session.commit()
        return int(released)


def lock_generation_task(session, job):
    task = session.scalar(select(Task).where(Task.id == job.task_id,
        Task.tenant_id == job.tenant_id).with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True))
    if (task is None or task.status != "running" or task.deleted_at is not None
            or task.retired_at is not None
            or int(task.task_lifecycle_epoch or 1) != int(job.task_lifecycle_epoch or 1)):
        raise RuntimeError("generation_result_task_inactive")
    return task


def require_prepared_window(session, job, action) -> None:
    if not job.window_slot_id:
        return
    slot = session.scalar(select(AiContentWindowPlanSlot).where(
        AiContentWindowPlanSlot.id == job.window_slot_id).with_for_update(nowait=True)
        .execution_options(populate_existing=True))
    if (slot is None or slot.state != "candidate_ready" or slot.claimed_by_job_id != job.id
            or (slot.obligation_type, slot.obligation_id, slot.account_id)
            != (job.obligation_type, job.obligation_id, action.account_id)
            or job.candidate_hash != action.candidate_hash):
        raise RuntimeError("parallel_generation_ready_window_invalid")


def prepare_recovered_candidate(session, job, action) -> None:
    lock_generation_task(session, job)
    require_ready_generation_action(job, action)
    if action.status not in {"executing", "pending"}:
        raise RuntimeError("parallel_generation_ready_action_invalid")
    require_prepared_window(session, job, action)
    release_generation_claim(action, dict(action.payload or {}))
    job.candidate_hash = action.candidate_hash
    job.evaluator_evidence = {**dict(job.evaluator_evidence or {}), "prepared_recovery": {
        "action_id": action.id, "candidate_hash": action.candidate_hash,
        "generation_lease_epoch": job.generation_lease_epoch,
    }}
    session.flush([job])


def _require_original_action_claim(action, claim) -> bool:
    payload = dict(action.payload or {})
    if owns_generation_claim(action, claim.owner, claim.token):
        if (action.lease_owner == claim.owner
                and payload.get("ai_generation_claim_owner") == claim.owner
                and payload.get("ai_generation_claim_token") == claim.token):
            return True
        raise RuntimeError("parallel_generation_action_claim_lost")
    if (action.status == "pending" and not action.claim_owner and not action.claim_token
            and not action.lease_owner and not payload.get("ai_generation_claim_owner")
            and not payload.get("ai_generation_claim_token")):
        return False
    raise RuntimeError("parallel_generation_action_claim_lost")


def _settlement_receipt(claim, action) -> dict:
    identity = [claim.action_id, claim.job_id, claim.owner, claim.token,
                claim.job_version, claim.generation_lease_epoch]
    digest = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return {"claim_hash": digest, "action_id": action.id, "candidate_hash": action.candidate_hash,
            "generation_lease_epoch": claim.generation_lease_epoch}


def _settlement_already_committed(job, receipt) -> bool:
    return bool(job.state == "ready" and not job.generation_owner_id
        and job.lease_expires_at is None and job.candidate_hash == receipt["candidate_hash"]
        and int(job.generation_lease_epoch or 0) == receipt["generation_lease_epoch"]
        and (job.evaluator_evidence or {}).get("generation_settlement") == receipt)
