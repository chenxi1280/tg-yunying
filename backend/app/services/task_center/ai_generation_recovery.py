from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, object_session

from app.models import (
    Action,
    AiContentWindowPlanSlot,
    ExecutionAttempt,
    GenerationJob,
    Task,
)
from app.services._common import _now

from .ai_content_runtime import invalidate_job_pre_gateway_slot
from .ai_generation_commit import commit_generation_action, load_generation_batch
from .ai_generation_pending_recovery import recover_pending_generation_residue
from .ai_generation_state import generation_result_cache, mark_attempt_outcome
from .ai_generation_timing import GENERATION_LEASE
from .ai_generation_unknown_recovery import (
    recover_cached_unknown_jobs,
    reset_generation_job_for_cached_retry as _reset_generation_job_for_cached_retry,
)
from .runtime_resources import _release_runtime_resources


logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class _ReconcileClaim:
    job_id: str
    owner: str
    previous_owner: str
    job_version: int
    generation_lease_epoch: int

def persist_generation_unknown(
    session: Session,
    request,
    contents: list[str],
    *,
    tokens: int,
    attempt_id: str,
    error_code: str = "",
    error_detail: str = "",
) -> None:
    logger.error(
        "AI generation result persistence failed action_id=%s error_type=%s detail=%s",
        request.action_id,
        error_code,
        error_detail,
    )
    batch = load_generation_batch(session, request)
    with session.no_autoflush:
        for index, ((action, payload), content) in enumerate(zip(batch, contents, strict=False)):
            data = payload.model_dump(mode="json")
            data["ai_generation_status"] = "ai_result_persist_unknown"
            data["ai_generation_result_cache"] = generation_result_cache(
                content,
                int(tokens or 0) if index == 0 else 0,
                attempt_id,
                payload=payload,
            )
            mark_attempt_outcome(
                data,
                attempt_id,
                "ai_result_persist_unknown",
                timestamp=_now(),
            )
            _reset_action_for_recovery(action, data)
            _reset_generation_job_for_cached_retry(session, data)
            action.result = {
                **dict(action.result or {}),
                "error_code": "ai_result_persist_unknown",
                "error_message": error_detail,
                "generation_outcome": "pending",
                "persist_error_code": error_code,
            }
            commit_generation_action(session, request, action)


def _reset_action_for_recovery(
    action: Action,
    data: dict,
    *,
    clear_claim: bool = True,
) -> None:
    action.payload = data
    action.status = "pending"
    if clear_claim:
        action.claim_owner = ""
        action.claim_token = ""
        action.claim_expires_at = None
    action.lease_owner = ""
    action.lease_expires_at = None
    action.result = {
        **(action.result or {}),
        "generation_stage": "ai_result_persist_unknown",
        "generation_outcome": "ai_result_persist_unknown",
    }
    _release_runtime_resources(action)


def recover_stale_pre_gateway_generation(
    action: Action,
    session: Session | None = None,
) -> bool:
    data = dict(action.payload or {})
    if not _is_generating_ai_action(action, data):
        return False
    sess = session or object_session(action)
    job = _find_generation_job(sess, action, data) if sess is not None else None
    attempt_id = str(data.get("ai_generation_attempt_id") or "")
    expected_owner = _payload_claim_owner(data)
    provider_started = bool((action.result or {}).get("ai_provider_call_started_at"))
    if provider_started:
        _mark_action_generation_unknown(action, data, attempt_id, clear_claim=False)
        if job is not None and sess is not None:
            _transition_job(
                sess, job,
                expected_owner=expected_owner,
                state="unknown", stage="ai_result_persist_unknown",
            )
        return True
    _mark_action_generation_pending(action, data, attempt_id)
    if job is not None and sess is not None:
        _transition_job(
            sess, job,
            expected_owner=expected_owner,
            state="pending", stage="generation_recovery",
        )
    return True


def reconcile_generation_jobs(session: Session, limit: int = 20) -> int:
    bounded_limit = max(1, int(limit))
    owner = f"generation-reconcile:{uuid4()}"
    reconciled = 0
    for job_id in _expired_job_ids(session, bounded_limit):
        claim = _claim_expired_job(session, job_id, owner)
        if claim is None:
            continue
        _reconcile_claimed_job(session, claim)
        reconciled += 1
    remaining = bounded_limit - reconciled
    if remaining > 0:
        reconciled += recover_pending_generation_residue(
            session, remaining, action_resolver=_current_generation_action)
    remaining = bounded_limit - reconciled
    if remaining > 0:
        reconciled += recover_cached_unknown_jobs(
            session, remaining, action_resolver=_current_generation_action,
        )
    return reconciled


def _expired_job_ids(session: Session, limit: int) -> tuple[str, ...]:
    return tuple(session.scalars(select(GenerationJob.id).where(
        GenerationJob.state == "generating",
        GenerationJob.lease_expires_at <= _now(),
    ).order_by(GenerationJob.lease_expires_at, GenerationJob.id).limit(limit)))


def _claim_expired_job(
    session: Session,
    job_id: str,
    owner: str,
) -> _ReconcileClaim | None:
    job = session.get(GenerationJob, job_id)
    now_value = _now()
    if job is None or job.lease_expires_at is None:
        return None
    version = int(job.job_version or 1)
    epoch = int(job.generation_lease_epoch or 0)
    previous_owner = str(job.generation_owner_id or "")
    changed = session.execute(update(GenerationJob).where(
        GenerationJob.id == job_id,
        GenerationJob.state == "generating",
        GenerationJob.generation_owner_id == job.generation_owner_id,
        GenerationJob.generation_lease_epoch == epoch,
        GenerationJob.job_version == version,
        GenerationJob.lease_expires_at <= now_value,
    ).values(
        generation_owner_id=owner,
        generation_lease_epoch=epoch + 1,
        lease_expires_at=now_value + GENERATION_LEASE,
        job_version=version + 1,
    ).execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        return None
    session.expire(job)
    return _ReconcileClaim(job_id, owner, previous_owner, version + 1, epoch + 1)


def _reconcile_claimed_job(session: Session, claim: _ReconcileClaim) -> None:
    job = session.get(GenerationJob, claim.job_id)
    if not _owns_reconcile_claim(job, claim):
        raise RuntimeError("generation_reconcile_claim_lost")
    task = session.get(Task, job.task_id)
    action = _current_generation_action(session, job)
    if action is not None and action.status == "success":
        _finish_reconciled_job(
            session, job, claim=claim, state="ready", stage="action_success",
        )
        return
    if _remote_boundary_exists(session, job, action):
        _finish_reconciled_job(
            session, job, claim=claim,
            state="unknown", stage="gateway_reconcile_required",
        )
        return
    if (
        action is not None
        and _is_generating_ai_action(action, dict(action.payload or {}))
        and (action.result or {}).get("ai_provider_call_started_at")
    ):
        _recover_claimed_action(session, action, job, claim=claim)
        return
    if not _task_matches_job(task, job):
        _finish_reconciled_job(
            session, job, claim=claim,
            state="cancelled", stage="lifecycle_expired",
        )
        return
    if action is None:
        _finish_reconciled_job(
            session, job, claim=claim,
            state="cancelled", stage="action_missing",
        )
        return
    if action.status in {"failed", "skipped"}:
        _finish_reconciled_job(
            session, job, claim=claim,
            state="cancelled", stage=f"action_{action.status}",
        )
        return
    _recover_claimed_action(session, action, job, claim=claim)


def _recover_claimed_action(
    session: Session,
    action: Action,
    job: GenerationJob,
    *,
    claim: _ReconcileClaim,
) -> None:
    data = dict(action.payload or {})
    if not _is_generating_ai_action(action, data):
        _finish_reconciled_job(
            session, job, claim=claim,
            state="cancelled", stage="action_state_mismatch",
        )
        return
    if not _action_owned_by_expired_worker(action, data, claim.previous_owner):
        raise RuntimeError("generation_reconcile_action_claim_changed")
    attempt_id = str(data.get("ai_generation_attempt_id") or "")
    if (action.result or {}).get("ai_provider_call_started_at"):
        _mark_action_generation_unknown(action, data, attempt_id, clear_claim=True)
        _finish_reconciled_job(
            session, job, claim=claim,
            state="unknown", stage="ai_result_persist_unknown",
        )
        return
    _mark_action_generation_pending(action, data, attempt_id)
    _finish_reconciled_job(
        session, job, claim=claim,
        state="pending", stage="generation_recovery",
    )


def _finish_reconciled_job(
    session: Session,
    job: GenerationJob,
    *,
    claim: _ReconcileClaim,
    state: str,
    stage: str,
) -> None:
    changed = session.execute(update(GenerationJob).where(
        GenerationJob.id == claim.job_id,
        GenerationJob.state == "generating",
        GenerationJob.generation_owner_id == claim.owner,
        GenerationJob.generation_lease_epoch == claim.generation_lease_epoch,
        GenerationJob.job_version == claim.job_version,
    ).values(
        state=state,
        generation_stage=stage,
        generation_owner_id="",
        lease_expires_at=None,
        next_retry_at=None,
        job_version=claim.job_version + 1,
    ).execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        raise RuntimeError("generation_reconcile_claim_lost")
    session.expire(job)
    session.refresh(job)
    if state == "cancelled":
        invalidate_job_pre_gateway_slot(session, job)


def _remote_boundary_exists(
    session: Session,
    job: GenerationJob,
    action: Action | None,
) -> bool:
    if job.window_slot_id:
        slot_state = session.scalar(select(AiContentWindowPlanSlot.state).where(
            AiContentWindowPlanSlot.id == job.window_slot_id,
        ))
        if slot_state == "gateway_bound":
            return True
    if action is None:
        return False
    if action.status == "unknown_after_send":
        return True
    return session.scalar(select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).limit(1)) is not None


def _current_generation_action(
    session: Session,
    job: GenerationJob,
) -> Action | None:
    job_id = Action.payload["generation_job_id"].as_string()
    return session.scalar(select(Action).where(
        Action.tenant_id == job.tenant_id,
        Action.task_id == job.task_id,
        Action.task_lifecycle_epoch == job.task_lifecycle_epoch,
        Action.obligation_type == job.obligation_type,
        Action.obligation_id == job.obligation_id,
        job_id == job.id,
    ).order_by(Action.action_version.desc(), Action.id.desc()).limit(1))


def _find_generation_job(
    session: Session,
    action: Action,
    data: dict,
) -> GenerationJob | None:
    job_id = str(data.get("generation_job_id") or "")
    if job_id:
        job = session.get(GenerationJob, job_id)
        if job is not None and not _job_matches_action(job, action):
            raise RuntimeError("generation_job_action_identity_mismatch")
        return job
    if not action.obligation_type or not action.obligation_id:
        return None
    return session.scalar(select(GenerationJob).where(
        GenerationJob.tenant_id == action.tenant_id,
        GenerationJob.task_id == action.task_id,
        GenerationJob.task_lifecycle_epoch == action.task_lifecycle_epoch,
        GenerationJob.obligation_type == action.obligation_type,
        GenerationJob.obligation_id == action.obligation_id,
        GenerationJob.state.in_(("pending", "generating", "unknown")),
    ).order_by(GenerationJob.created_at.desc(), GenerationJob.id.desc()).limit(1))


def _transition_job(
    session: Session,
    job: GenerationJob,
    *,
    expected_owner: str,
    state: str,
    stage: str,
) -> None:
    if not expected_owner:
        raise RuntimeError("generation_recovery_owner_missing")
    version = int(job.job_version or 1)
    changed = session.execute(update(GenerationJob).where(
        GenerationJob.id == job.id,
        GenerationJob.state == "generating",
        GenerationJob.generation_owner_id == expected_owner,
        GenerationJob.generation_lease_epoch == int(job.generation_lease_epoch or 0),
        GenerationJob.job_version == version,
    ).values(
        state=state,
        generation_stage=stage,
        generation_owner_id="",
        lease_expires_at=None,
        next_retry_at=None,
        job_version=version + 1,
    ).execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        raise RuntimeError("generation_recovery_job_claim_lost")
    session.expire(job)


def _mark_action_generation_unknown(
    action: Action,
    data: dict,
    attempt_id: str,
    *,
    clear_claim: bool,
) -> None:
    mark_attempt_outcome(data, attempt_id, "ai_result_persist_unknown", timestamp=_now())
    data["ai_generation_status"] = "ai_result_persist_unknown"
    _reset_action_for_recovery(action, data, clear_claim=clear_claim)
    action.executed_at = None


def _payload_claim_owner(data: dict) -> str:
    return str(data.get("ai_generation_claim_owner") or "")


def _job_matches_action(job: GenerationJob, action: Action) -> bool:
    core_matches = bool(
        job.tenant_id == action.tenant_id
        and job.task_id == action.task_id
        and int(job.task_lifecycle_epoch or 1) == int(action.task_lifecycle_epoch or 1)
    )
    if action.task_type != "group_ai_chat":
        return core_matches
    return bool(
        core_matches
        and job.obligation_type == action.obligation_type
        and job.obligation_id == action.obligation_id
    )


def _mark_action_generation_pending(
    action: Action,
    data: dict,
    attempt_id: str,
) -> None:
    mark_attempt_outcome(data, attempt_id, "stale_worker_recovered", timestamp=_now())
    data.update({
        "ai_generation_status": "pending",
        "ai_generation_attempt_id": "",
        "ai_generation_request_id": "",
        "ai_generation_claim_owner": "",
        "ai_generation_claim_token": "",
    })
    _reset_action_for_recovery(action, data)
    action.executed_at = None
    action.result = {
        **dict(action.result or {}),
        "generation_stage": "generation_recovery",
        "generation_outcome": "retry_pending",
        "recovered_ai_generation_attempt_id": attempt_id,
    }


def _owns_reconcile_claim(
    job: GenerationJob | None,
    claim: _ReconcileClaim,
) -> bool:
    return bool(
        job
        and job.state == "generating"
        and job.generation_owner_id == claim.owner
        and int(job.job_version or 1) == claim.job_version
        and int(job.generation_lease_epoch or 0) == claim.generation_lease_epoch
    )


def _action_owned_by_expired_worker(
    action: Action,
    data: dict,
    previous_owner: str,
) -> bool:
    if not previous_owner:
        return False
    payload_owner = str(data.get("ai_generation_claim_owner") or "")
    payload_token = str(data.get("ai_generation_claim_token") or "")
    no_live_owner = not action.claim_owner and not action.lease_owner
    if no_live_owner:
        return payload_owner in {"", previous_owner}
    return bool(
        action.claim_owner == previous_owner
        and action.lease_owner == previous_owner
        and payload_owner == previous_owner
        and action.claim_token
        and action.claim_token == payload_token
    )


def _task_matches_job(task: Task | None, job: GenerationJob) -> bool:
    return bool(
        task
        and task.status == "running"
        and task.deleted_at is None
        and int(task.task_lifecycle_epoch or 1) == int(job.task_lifecycle_epoch or 1)
    )


def _is_generating_ai_action(action: Action, data: dict) -> bool:
    generation_action = (
        (action.task_type == "group_ai_chat" and action.action_type == "send_message")
        or (action.task_type == "channel_comment" and action.action_type == "post_comment")
    )
    return generation_action and data.get("ai_generation_status") == "generating"


__all__ = ["persist_generation_unknown", "reconcile_generation_jobs", "recover_stale_pre_gateway_generation"]
