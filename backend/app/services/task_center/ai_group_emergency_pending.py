"""Settle only generation, retaining the original unsent message obligation."""
from types import SimpleNamespace

from sqlalchemy import select

from app.models import Action, GenerationJob, Task
from app.services._common import _now

from .ai_generation_claim_lifecycle import release_generation_claim
from .ai_generation_commit import commit_generation_action, load_generation_batch
from .ai_generation_job_finish import finish_owned_job
from .ai_generation_state import mark_attempt_outcome
from .ai_group_emergency_contract import PENDING_STATUS, emergency_enabled, telegram_unattempted


def persist_pre_request_emergency(session, task, action, *, reason: str) -> bool:
    if not emergency_enabled(task) or not _eligible_action(session, action):
        return False
    owner, token = action.claim_owner, action.claim_token
    locked = session.scalar(select(Action).where(Action.id == action.id,
        Action.status == "executing", Action.claim_owner == owner, Action.claim_token == token,
    ).with_for_update().execution_options(populate_existing=True))
    if locked is None or not owner or not token:
        raise RuntimeError("emergency_generation_claim_lost")
    request = SimpleNamespace(claim_owner=owner,
        attempt_id=str((locked.payload or {}).get("ai_generation_attempt_id") or ""))
    changed = mark_emergency_pending(session, request, action=locked, reason=reason)
    if changed:
        session.commit()
    return changed


def persist_emergency_batch(session, request, *, reason: str) -> bool:
    task = session.get(Task, request.task_id)
    if not task or not emergency_enabled(task):
        return False
    batch = load_generation_batch(session, request)
    if any(not _eligible_action(session, action) for action, _ in batch):
        return False
    with session.no_autoflush:
        for action, _ in batch:
            mark_emergency_pending(session, request, action=action, reason=reason)
            commit_generation_action(session, request, action)
    return True


def mark_emergency_pending(session, request, *, action, reason: str, evidence=None) -> bool:
    task = session.get(Task, action.task_id)
    if not task or not emergency_enabled(task) or not _eligible_action(session, action):
        return False
    job = session.scalar(select(GenerationJob).where(
        GenerationJob.id == str((action.payload or {}).get("generation_job_id") or ""),
    ).with_for_update())
    if job is None or not _job_matches(action, job):
        raise RuntimeError("emergency_generation_job_scope_invalid")
    claim = SimpleNamespace(action_id=action.id, job_id=job.id, owner=request.claim_owner,
                            job_version=job.job_version, generation_lease_epoch=job.generation_lease_epoch)
    finish_owned_job(session, claim, job=job, action=action, state="failed", generation_stage=PENDING_STATUS)
    job.evaluator_evidence = {**dict(job.evaluator_evidence or {}), "emergency_generation": {
        "reason": reason, "evidence": dict(evidence or {}),
    }}
    data = dict(action.payload or {})
    data["ai_generation_status"] = PENDING_STATUS
    data["ai_generation_result_cache"] = {}
    mark_attempt_outcome(data, request.attempt_id, reason, timestamp=_now())
    release_generation_claim(action, data)
    action.result = {**dict(action.result or {}), "success": False, "error_code": reason,
                     "generation_stage": reason, "generation_outcome": PENDING_STATUS,
                     "emergency_generation_evidence": dict(evidence or {})}
    return True


def _eligible_action(session, action) -> bool:
    return bool(action.task_type == "group_ai_chat" and action.action_type == "send_message"
                and action.primary_quantity_slot_id and not (action.payload or {}).get("message_text")
                and telegram_unattempted(session, action))


def _job_matches(action, job) -> bool:
    return ((job.tenant_id, job.task_id, job.task_lifecycle_epoch, job.obligation_type, job.obligation_id)
            == (action.tenant_id, action.task_id, action.task_lifecycle_epoch,
                action.obligation_type, action.obligation_id))
