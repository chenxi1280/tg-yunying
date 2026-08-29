from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountPacingReservation,
    Action,
    FulfillmentObligationProjection,
    GenerationJob,
    Task,
)
from app.services._common import _now

from .ai_content_runtime import (
    ShortfallSpec,
    defer_generation_job,
    invalidate_job_pre_gateway_slot,
    settle_shortfall,
)
from .datetime_compat import is_after_or_equal
from .generation_shortfall_projection import project_generation_shortfall


DEFAULT_GENERATION_RETRY_SECONDS = 60


@dataclass(frozen=True)
class GenerationWaitSpec:
    stage: str
    error_code: str
    error_detail: str
    shortfall_kind: str
    evaluator_evidence: dict
    next_retry_at: datetime | None = None
    retry_budget_exhausted: bool = False


def latest_safe_send_at(session: Session, action: Action) -> datetime | None:
    reservation = session.scalar(select(AccountPacingReservation).where(
        AccountPacingReservation.action_id == action.id,
    ))
    if reservation is not None and reservation.source_deadline_at is not None:
        return reservation.source_deadline_at
    projection = session.scalar(select(FulfillmentObligationProjection).where(
        FulfillmentObligationProjection.obligation_type == action.obligation_type,
        FulfillmentObligationProjection.obligation_id == action.obligation_id,
    ))
    if projection is not None:
        return projection.deadline_at
    return _payload_deadline(action)


def defer_generation_wait(
    session: Session,
    task: Task,
    action: Action,
    job: GenerationJob,
    spec: GenerationWaitSpec,
) -> str:
    now_value = _now()
    next_retry_at = spec.next_retry_at or (
        now_value + timedelta(seconds=_retry_seconds(task))
    )
    deadline = job.latest_safe_send_at or latest_safe_send_at(session, action)
    job.latest_safe_send_at = deadline
    job.candidate_hash = str(action.candidate_hash or "")
    job.evaluator_evidence = dict(spec.evaluator_evidence)
    if spec.retry_budget_exhausted:
        _settle_generation_shortfall(
            session,
            action,
            job=job,
            spec=spec,
            reason_suffix="budget_exhausted",
        )
        return "shortfall"
    if deadline is not None and is_after_or_equal(next_retry_at, deadline):
        _settle_generation_shortfall(
            session,
            action,
            job=job,
            spec=spec,
            reason_suffix="deadline",
        )
        return "shortfall"
    defer_generation_job(job, stage=spec.stage, next_retry_at=next_retry_at)
    _set_action_waiting(action, spec, next_retry_at)
    return "deferred"


def _set_action_waiting(
    action: Action,
    spec: GenerationWaitSpec,
    next_retry_at: datetime,
) -> None:
    payload = dict(action.payload or {})
    payload["ai_generation_status"] = "pending"
    payload["ai_generation_claim_owner"] = ""
    payload["ai_generation_claim_token"] = ""
    action.payload = payload
    action.status = "pending"
    action.executed_at = None
    _clear_claim(action)
    action.result = {
        **dict(action.result or {}),
        **_result(spec, "pending", next_retry_at=next_retry_at),
    }


def _settle_generation_shortfall(
    session: Session,
    action: Action,
    *,
    job: GenerationJob,
    spec: GenerationWaitSpec,
    reason_suffix: str,
) -> None:
    reason_code = f"{spec.error_code}_{reason_suffix}"
    _record_generation_shortfall(
        session,
        action,
        job=job,
        spec=spec,
        reason_code=reason_code,
    )
    invalidate_job_pre_gateway_slot(session, job)
    _mark_job_shortfall(job, spec)
    _mark_action_shortfall(session, action, spec=spec, reason_code=reason_code)


def _record_generation_shortfall(
    session: Session,
    action: Action,
    *,
    job: GenerationJob,
    spec: GenerationWaitSpec,
    reason_code: str,
) -> None:
    period_key = _shortfall_period(job, spec)
    evidence_hash = _hash({
        "job_id": job.id,
        "deadline": job.latest_safe_send_at,
        "code": reason_code,
        "evidence": spec.evaluator_evidence,
    })
    settle_shortfall(session, ShortfallSpec(
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        task_lifecycle_epoch=int(action.task_lifecycle_epoch or 1),
        owner_type=job.obligation_type,
        owner_id=job.obligation_id,
        period_key=period_key,
        kind=spec.shortfall_kind,
        reason_code=reason_code,
        requested_quantity=1,
        settled_quantity=0,
        evidence_hash=evidence_hash,
    ))


def _mark_job_shortfall(job: GenerationJob, spec: GenerationWaitSpec) -> None:
    job.state = "failed"
    job.generation_stage = f"{spec.stage}_shortfall"
    job.next_retry_at = None
    job.generation_owner_id = ""
    job.lease_expires_at = None
    job.evaluator_evidence = dict(spec.evaluator_evidence)
    job.job_version += 1


def _mark_action_shortfall(
    session: Session,
    action: Action,
    *,
    spec: GenerationWaitSpec,
    reason_code: str,
) -> None:
    payload = dict(action.payload or {})
    payload["ai_generation_status"] = reason_code
    payload["ai_generation_claim_owner"] = ""
    payload["ai_generation_claim_token"] = ""
    action.payload = payload
    action.status = "failed"
    action.executed_at = _now()
    _clear_claim(action)
    action.result = {**dict(action.result or {}), **_result(spec, "shortfall")}
    with session.no_autoflush:
        project_generation_shortfall(
            session,
            action,
            reason_code=reason_code,
        )


def _result(
    spec: GenerationWaitSpec,
    outcome: str,
    *,
    next_retry_at: datetime | None = None,
) -> dict:
    result = {
        "success": False,
        "error_code": spec.error_code,
        "error_message": spec.error_detail,
        "generation_stage": spec.stage,
        "generation_outcome": outcome,
        "evaluator_evidence": dict(spec.evaluator_evidence),
    }
    if next_retry_at is not None:
        result["next_retry_at"] = next_retry_at.isoformat()
    return result


def _clear_claim(action: Action) -> None:
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.lease_owner = ""
    action.lease_expires_at = None


def _retry_seconds(task: Task) -> int:
    value = dict(task.failure_policy or {}).get("retry_delay_seconds")
    return max(1, int(value if value is not None else DEFAULT_GENERATION_RETRY_SECONDS))


def _payload_deadline(action: Action) -> datetime | None:
    payload = dict(action.payload or {})
    raw = payload.get("obligation_deadline_at") or payload.get("deadline_at")
    if raw is None or isinstance(raw, datetime):
        return raw
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


def _shortfall_period(job: GenerationJob, spec: GenerationWaitSpec) -> str:
    return f"generation:{job.generation_sequence}:{spec.shortfall_kind}"[:80]


def _hash(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "GenerationWaitSpec",
    "defer_generation_wait",
    "latest_safe_send_at",
]
