from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AiAccountVoiceProfileGenerationAttempt,
    AiAccountVoiceProfileGenerationItem,
    AiAccountVoiceProfileGenerationJob,
)

from .account_voice_profile_generation_jobs import (
    VoiceProfileGenerationQueueResult,
    _new_job,
    enqueue_voice_profile_generation,
)
from .account_voice_profile_generation_reconcile import missing_profile_accounts


@dataclass(frozen=True)
class VoiceProfileGenerationJobCreateResult:
    job: AiAccountVoiceProfileGenerationJob
    queue: VoiceProfileGenerationQueueResult


def create_voice_profile_generation_job(
    session: Session,
    *,
    tenant_id: int,
    mode: str,
    account_ids: list[int],
    rebuild_existing: bool,
    reason: str,
    idempotency_key: str,
    actor: str,
) -> VoiceProfileGenerationJobCreateResult:
    existing = _idempotent_job(session, tenant_id, idempotency_key)
    if existing is not None:
        return VoiceProfileGenerationJobCreateResult(existing, _existing_job_queue(existing))
    target_ids = _target_account_ids(session, tenant_id, mode, account_ids)
    source = "manual_single" if len(target_ids) == 1 else "manual_batch"
    job, existing = _idempotent_or_new_job(
        session, tenant_id, source, reason, idempotency_key, actor,
    )
    if existing:
        return VoiceProfileGenerationJobCreateResult(job, _existing_job_queue(job))
    queue = enqueue_voice_profile_generation(
        session,
        tenant_id=tenant_id,
        account_ids=target_ids,
        source=source,
        actor=actor,
        reason=reason,
        rebuild_existing=rebuild_existing,
        job=job,
    )
    return VoiceProfileGenerationJobCreateResult(job, queue)


def _idempotent_or_new_job(
    session: Session,
    tenant_id: int,
    source: str,
    reason: str,
    idempotency_key: str,
    actor: str,
) -> tuple[AiAccountVoiceProfileGenerationJob, bool]:
    existing = _idempotent_job(session, tenant_id, idempotency_key)
    if existing is not None:
        return existing, True
    try:
        with session.begin_nested():
            job = _new_job(session, tenant_id, source, actor, reason, idempotency_key)
    except IntegrityError:
        existing = _idempotent_job(session, tenant_id, idempotency_key)
        if existing is None:
            raise
        return existing, True
    return job, False


def list_voice_profile_generation_jobs(
    session: Session,
    *,
    tenant_id: int,
    status: str = "",
    account_id: int | None = None,
    offset: int = 0,
    limit: int = 100,
) -> list[dict]:
    statement = select(AiAccountVoiceProfileGenerationJob).where(
        AiAccountVoiceProfileGenerationJob.tenant_id == tenant_id,
    )
    if status:
        statement = statement.where(AiAccountVoiceProfileGenerationJob.status == status)
    if account_id is not None:
        statement = statement.where(
            AiAccountVoiceProfileGenerationJob.id.in_(
                select(AiAccountVoiceProfileGenerationItem.job_id).where(
                    AiAccountVoiceProfileGenerationItem.tenant_id == tenant_id,
                    AiAccountVoiceProfileGenerationItem.account_id == account_id,
                )
            )
        )
    rows = session.scalars(
        statement.order_by(AiAccountVoiceProfileGenerationJob.created_at.desc()).offset(max(0, offset)).limit(_page_limit(limit))
    )
    return [_job_projection(job) for job in rows]


def voice_profile_generation_job_detail(session: Session, *, tenant_id: int, job_id: str) -> dict:
    job = session.scalar(
        select(AiAccountVoiceProfileGenerationJob).where(
            AiAccountVoiceProfileGenerationJob.id == job_id,
            AiAccountVoiceProfileGenerationJob.tenant_id == tenant_id,
        )
    )
    if job is None:
        raise LookupError("voice profile generation job not found")
    items = list(session.scalars(
        select(AiAccountVoiceProfileGenerationItem)
        .where(AiAccountVoiceProfileGenerationItem.job_id == job.id)
        .order_by(AiAccountVoiceProfileGenerationItem.created_at.asc(), AiAccountVoiceProfileGenerationItem.id.asc())
    ))
    attempts = _attempts_by_item(session, [item.id for item in items])
    return _job_projection(job) | {"items": [_item_projection(item, attempts.get(item.id, [])) for item in items]}


def voice_profile_generation_item_detail(session: Session, *, tenant_id: int, item_id: str) -> dict:
    item = session.scalar(
        select(AiAccountVoiceProfileGenerationItem).where(
            AiAccountVoiceProfileGenerationItem.id == item_id,
            AiAccountVoiceProfileGenerationItem.tenant_id == tenant_id,
        )
    )
    if item is None:
        raise LookupError("voice profile generation item not found")
    attempts = _attempts_by_item(session, [item.id])
    return _item_projection(item, attempts.get(item.id, []))


def _idempotent_job(session: Session, tenant_id: int, idempotency_key: str) -> AiAccountVoiceProfileGenerationJob | None:
    if not idempotency_key.strip():
        raise ValueError("idempotency_key is required")
    return session.scalar(
        select(AiAccountVoiceProfileGenerationJob).where(
            AiAccountVoiceProfileGenerationJob.tenant_id == tenant_id,
            AiAccountVoiceProfileGenerationJob.idempotency_key == idempotency_key,
        )
    )


def _target_account_ids(session: Session, tenant_id: int, mode: str, account_ids: list[int]) -> list[int]:
    unique_ids = list(dict.fromkeys(int(account_id) for account_id in account_ids))
    if mode == "selected":
        if not unique_ids:
            raise ValueError("account_ids is required for selected mode")
        return unique_ids
    if mode != "missing":
        raise ValueError("mode must be selected or missing")
    return missing_profile_accounts(session, tenant_id, 1000).get(tenant_id, [])


def _existing_job_queue(job: AiAccountVoiceProfileGenerationJob) -> VoiceProfileGenerationQueueResult:
    return VoiceProfileGenerationQueueResult(job.id, (), (), (), None)


def _attempts_by_item(session: Session, item_ids: list[str]) -> dict[str, list[dict]]:
    if not item_ids:
        return {}
    rows = session.scalars(
        select(AiAccountVoiceProfileGenerationAttempt)
        .where(AiAccountVoiceProfileGenerationAttempt.item_id.in_(item_ids))
        .order_by(AiAccountVoiceProfileGenerationAttempt.attempt_no.asc())
    )
    result: dict[str, list[dict]] = {}
    for attempt in rows:
        result.setdefault(attempt.item_id, []).append(_attempt_projection(attempt))
    return result


def _job_projection(job: AiAccountVoiceProfileGenerationJob) -> dict:
    return {
        "id": job.id,
        "source": job.source,
        "status": job.status,
        "requested_by": job.requested_by,
        "reason": job.reason,
        "total_count": job.total_count,
        "succeeded_count": job.succeeded_count,
        "retry_wait_count": job.retry_wait_count,
        "failed_count": job.failed_count,
        "skipped_count": job.skipped_count,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def _item_projection(item: AiAccountVoiceProfileGenerationItem, attempts: list[dict]) -> dict:
    return {
        "id": item.id,
        "account_id": item.account_id,
        "status": item.status,
        "source": item.source,
        "expected_profile_version": item.expected_profile_version,
        "base_profile_version": item.base_profile_version,
        "result_profile_version": item.result_profile_version,
        "attempt_count": item.attempt_count,
        "next_retry_at": item.next_retry_at,
        "error_code": item.error_code,
        "error_detail": item.error_detail,
        "previous_item_id": item.previous_item_id,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "finished_at": item.finished_at,
        "attempts": attempts,
    }


def _attempt_projection(attempt: AiAccountVoiceProfileGenerationAttempt) -> dict:
    return {
        "id": attempt.id,
        "attempt_no": attempt.attempt_no,
        "stage": attempt.stage,
        "provider": attempt.provider,
        "provider_request_id": attempt.provider_request_id,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
        "outcome": attempt.outcome,
        "error_code": attempt.error_code,
        "error_detail": attempt.error_detail,
        "prompt_feedback_summary": attempt.prompt_feedback_summary,
    }


def _page_limit(value: int) -> int:
    return min(200, max(1, int(value)))


__all__ = [
    "VoiceProfileGenerationJobCreateResult",
    "create_voice_profile_generation_job",
    "list_voice_profile_generation_jobs",
    "voice_profile_generation_item_detail",
    "voice_profile_generation_job_detail",
]
