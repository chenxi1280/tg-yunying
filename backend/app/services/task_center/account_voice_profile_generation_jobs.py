from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AiAccountVoiceProfile,
    AiAccountVoiceProfileGenerationItem,
    AiAccountVoiceProfileGenerationJob,
    AuditLog,
    OPEN_GENERATION_ITEM_STATUSES,
    TgAccount,
)
from app.services._common import _now
from . import account_voice_profile_usage as voice_usage
from .account_voice_profile_generation_reconcile import (
    manual_required_missing_profile_accounts,
    missing_profile_accounts,
)


@dataclass(frozen=True)
class VoiceProfileGenerationQueueResult:
    job_id: str | None
    created_account_ids: tuple[int, ...]
    existing_account_ids: tuple[int, ...]
    skipped_account_ids: tuple[int, ...]
    next_retry_at: datetime | None
    manual_required_account_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class VoiceProfileGenerationReconcileResult:
    created_account_ids: tuple[int, ...]
    existing_account_ids: tuple[int, ...]
    skipped_account_ids: tuple[int, ...]
    manual_required_account_ids: tuple[int, ...]


def enqueue_voice_profile_generation(
    session: Session,
    *,
    tenant_id: int,
    account_ids: list[int],
    source: str,
    actor: str,
    reason: str = "",
    rebuild_existing: bool = False,
    job: AiAccountVoiceProfileGenerationJob | None = None,
) -> VoiceProfileGenerationQueueResult:
    unique_ids = tuple(dict.fromkeys(int(account_id) for account_id in account_ids))
    _require_accounts(session, tenant_id, unique_ids)
    created: list[int] = []
    existing: list[int] = []
    skipped: list[int] = []
    manual_required: list[int] = []
    active_job = job
    for account_id in unique_ids:
        outcome = _enqueue_account(session, tenant_id, account_id, source, actor, reason, active_job, rebuild_existing)
        active_job = outcome.job or active_job
        _append_outcome(outcome.kind, account_id, created, existing, skipped, manual_required)
    if active_job is not None:
        refresh_generation_job(session, active_job)
        _audit_queue(session, tenant_id, actor, source, active_job, created, existing)
    retry_at = _next_retry_at(session, tenant_id, (*created, *existing))
    return VoiceProfileGenerationQueueResult(
        active_job.id if active_job else None,
        tuple(created),
        tuple(existing),
        tuple(skipped),
        retry_at,
        tuple(manual_required),
    )


@dataclass(frozen=True)
class _QueueOutcome:
    kind: str
    job: AiAccountVoiceProfileGenerationJob | None


def _enqueue_account(
    session: Session,
    tenant_id: int,
    account_id: int,
    source: str,
    actor: str,
    reason: str,
    job: AiAccountVoiceProfileGenerationJob | None,
    rebuild_existing: bool,
) -> _QueueOutcome:
    profile_state = _profile_state(session, tenant_id, account_id)
    if profile_state == "disabled" or (profile_state == "usable" and not rebuild_existing):
        return _QueueOutcome("skipped", job)
    if _open_item(session, tenant_id, account_id) is not None:
        return _QueueOutcome("existing", job)
    if _manual_required_item(session, tenant_id, account_id) is not None:
        return _QueueOutcome("manual_required", job)
    active_job = job or _new_job(session, tenant_id, source, actor, reason)
    if _create_item(session, active_job, tenant_id, account_id, source):
        return _QueueOutcome("created", active_job)
    if job is None:
        session.delete(active_job)
    return _QueueOutcome("existing", job)


def _require_accounts(session: Session, tenant_id: int, account_ids: tuple[int, ...]) -> None:
    for account_id in account_ids:
        account = session.get(TgAccount, account_id)
        if account is None or account.tenant_id != tenant_id or account.deleted_at is not None:
            raise ValueError(f"account not found: {account_id}")
        voice_usage.assert_voice_profile_mutation_allowed(session, account)


def _profile_state(session: Session, tenant_id: int, account_id: int) -> str:
    profile = session.scalar(
        select(AiAccountVoiceProfile)
        .where(AiAccountVoiceProfile.tenant_id == tenant_id, AiAccountVoiceProfile.account_id == account_id)
        .order_by(AiAccountVoiceProfile.version.desc())
        .limit(1)
    )
    if profile is None:
        return "missing"
    if profile.status == "disabled":
        return "disabled"
    if profile.status != "active" or profile.quality_status != "active" or int(profile.version or 0) < 1:
        return "missing"
    summary = str(profile.short_prompt_summary or "").strip()
    if not summary:
        return "missing"
    generic_hits = sum(term in summary for term in ("自然", "随意", "真实", "像真人"))
    return "missing" if generic_hits >= 2 else "usable"


def _open_item(session: Session, tenant_id: int, account_id: int) -> AiAccountVoiceProfileGenerationItem | None:
    return session.scalar(
        select(AiAccountVoiceProfileGenerationItem)
        .where(
            AiAccountVoiceProfileGenerationItem.tenant_id == tenant_id,
            AiAccountVoiceProfileGenerationItem.account_id == account_id,
            AiAccountVoiceProfileGenerationItem.status.in_(OPEN_GENERATION_ITEM_STATUSES),
        )
        .order_by(AiAccountVoiceProfileGenerationItem.created_at.asc())
        .limit(1)
    )


def _manual_required_item(session: Session, tenant_id: int, account_id: int) -> AiAccountVoiceProfileGenerationItem | None:
    return session.scalar(
        select(AiAccountVoiceProfileGenerationItem)
        .where(
            AiAccountVoiceProfileGenerationItem.tenant_id == tenant_id,
            AiAccountVoiceProfileGenerationItem.account_id == account_id,
            AiAccountVoiceProfileGenerationItem.status == "manual_required",
        )
        .order_by(AiAccountVoiceProfileGenerationItem.updated_at.desc(), AiAccountVoiceProfileGenerationItem.id.desc())
        .limit(1)
    )


def _new_job(
    session: Session,
    tenant_id: int,
    source: str,
    actor: str,
    reason: str,
    idempotency_key: str | None = None,
) -> AiAccountVoiceProfileGenerationJob:
    job = AiAccountVoiceProfileGenerationJob(
        tenant_id=tenant_id,
        source=source,
        idempotency_key=idempotency_key,
        requested_by=actor,
        reason=reason,
    )
    session.add(job)
    session.flush()
    return job


def _create_item(
    session: Session,
    job: AiAccountVoiceProfileGenerationJob,
    tenant_id: int,
    account_id: int,
    source: str,
) -> bool:
    expected_version = _expected_profile_version(session, tenant_id, account_id)
    item = AiAccountVoiceProfileGenerationItem(
        job_id=job.id,
        tenant_id=tenant_id,
        account_id=account_id,
        source=source,
        expected_profile_version=expected_version,
        base_profile_version=max(0, expected_version - 1),
        idempotency_key=f"voice-profile:{tenant_id}:{account_id}:{expected_version}",
        next_retry_at=_now(),
    )
    try:
        with session.begin_nested():
            session.add(item)
            session.flush()
        return True
    except IntegrityError:
        return False


def _expected_profile_version(session: Session, tenant_id: int, account_id: int) -> int:
    version = session.scalar(
        select(func.max(AiAccountVoiceProfile.version)).where(
            AiAccountVoiceProfile.tenant_id == tenant_id,
            AiAccountVoiceProfile.account_id == account_id,
        )
    )
    return max(1, int(version or 0) + 1)


def _next_retry_at(session: Session, tenant_id: int, account_ids: tuple[int, ...]) -> datetime | None:
    if not account_ids:
        return None
    return session.scalar(
        select(func.min(AiAccountVoiceProfileGenerationItem.next_retry_at)).where(
            AiAccountVoiceProfileGenerationItem.tenant_id == tenant_id,
            AiAccountVoiceProfileGenerationItem.account_id.in_(account_ids),
            AiAccountVoiceProfileGenerationItem.status.in_(OPEN_GENERATION_ITEM_STATUSES),
        )
    )


def _append_outcome(
    kind: str,
    account_id: int,
    created: list[int],
    existing: list[int],
    skipped: list[int],
    manual_required: list[int],
) -> None:
    {"created": created, "existing": existing, "skipped": skipped, "manual_required": manual_required}[kind].append(account_id)


def retry_voice_profile_generation_item(
    session: Session,
    *,
    tenant_id: int,
    item_id: str,
    expected_status: str,
    expected_profile_version: int,
    idempotency_key: str,
    reason: str,
    actor: str,
) -> AiAccountVoiceProfileGenerationItem:
    existing = _operator_retry_item(session, tenant_id, idempotency_key)
    if existing is not None:
        _assert_retry_idempotency_matches(existing, item_id)
        return existing
    item = _retryable_item(session, tenant_id, item_id, expected_status, expected_profile_version)
    _require_accounts(session, tenant_id, (item.account_id,))
    if item.status == "retry_wait":
        return _requeue_retry_wait_item(session, item, idempotency_key, reason, actor)
    return _create_manual_retry_item(session, item, idempotency_key, reason, actor)


def _operator_retry_item(
    session: Session,
    tenant_id: int,
    idempotency_key: str,
) -> AiAccountVoiceProfileGenerationItem | None:
    if not idempotency_key.strip():
        raise ValueError("idempotency_key is required")
    return session.scalar(
        select(AiAccountVoiceProfileGenerationItem).where(
            AiAccountVoiceProfileGenerationItem.tenant_id == tenant_id,
            AiAccountVoiceProfileGenerationItem.operator_idempotency_key == idempotency_key,
        )
    )


def _assert_retry_idempotency_matches(item: AiAccountVoiceProfileGenerationItem, item_id: str) -> None:
    if item.id != item_id and item.previous_item_id != item_id:
        raise ValueError("voice profile retry idempotency key belongs to another item")


def _retryable_item(
    session: Session,
    tenant_id: int,
    item_id: str,
    expected_status: str,
    expected_profile_version: int,
) -> AiAccountVoiceProfileGenerationItem:
    item = session.scalar(
        select(AiAccountVoiceProfileGenerationItem).where(
            AiAccountVoiceProfileGenerationItem.id == item_id,
            AiAccountVoiceProfileGenerationItem.tenant_id == tenant_id,
        )
    )
    if item is None:
        raise LookupError("voice profile generation item not found")
    if item.status != expected_status or int(item.expected_profile_version) != int(expected_profile_version):
        raise ValueError("voice profile generation retry conflict")
    if item.status not in {"retry_wait", "manual_required"}:
        raise ValueError("voice profile generation item is not retryable")
    if _expected_profile_version(session, tenant_id, item.account_id) != int(item.expected_profile_version):
        raise ValueError("voice profile generation retry profile version conflict")
    return item


def _requeue_retry_wait_item(
    session: Session,
    item: AiAccountVoiceProfileGenerationItem,
    idempotency_key: str,
    reason: str,
    actor: str,
) -> AiAccountVoiceProfileGenerationItem:
    item.status = "queued"
    item.next_retry_at = _now()
    item.finished_at = None
    item.lease_owner = ""
    item.lease_expires_at = None
    item.operator_idempotency_key = idempotency_key
    _audit_manual_retry(session, item, actor, reason)
    _refresh_generation_item_job(session, item.job_id)
    return item


def _create_manual_retry_item(
    session: Session,
    item: AiAccountVoiceProfileGenerationItem,
    idempotency_key: str,
    reason: str,
    actor: str,
) -> AiAccountVoiceProfileGenerationItem:
    job = _new_job(session, item.tenant_id, "manual_single", actor, reason)
    expected_version = _expected_profile_version(session, item.tenant_id, item.account_id)
    retry = AiAccountVoiceProfileGenerationItem(
        job_id=job.id,
        tenant_id=item.tenant_id,
        account_id=item.account_id,
        source="manual_single",
        previous_item_id=item.id,
        operator_idempotency_key=idempotency_key,
        expected_profile_version=expected_version,
        base_profile_version=max(0, expected_version - 1),
        idempotency_key=f"manual-retry:{item.tenant_id}:{item.account_id}:{job.id}",
        next_retry_at=_now(),
    )
    session.add(retry)
    session.flush()
    refresh_generation_job(session, job)
    _audit_manual_retry(session, retry, actor, reason)
    return retry


def _audit_manual_retry(
    session: Session,
    item: AiAccountVoiceProfileGenerationItem,
    actor: str,
    reason: str,
) -> None:
    session.add(
        AuditLog(
            tenant_id=item.tenant_id,
            actor=actor,
            action="账号面具已人工重试",
            target_type="ai_account_voice_profile_generation_item",
            target_id=item.id,
            detail=f"previous_item_id={item.previous_item_id or item.id};reason={reason}",
        )
    )


def reconcile_missing_voice_profile_generation(
    session: Session,
    *,
    tenant_id: int | None = None,
    limit: int = 100,
    actor: str = "voice-profile-reconcile",
) -> VoiceProfileGenerationReconcileResult:
    candidates = missing_profile_accounts(session, tenant_id, limit)
    results = [
        _reconcile_tenant(session, item_tenant_id, account_ids, actor)
        for item_tenant_id, account_ids in candidates.items()
    ]
    manual_required = manual_required_missing_profile_accounts(session, tenant_id, limit)
    return _combine_reconcile_results(results, manual_required)


def _reconcile_tenant(session: Session, tenant_id: int, account_ids: list[int], actor: str) -> VoiceProfileGenerationQueueResult:
    return enqueue_voice_profile_generation(
        session,
        tenant_id=tenant_id,
        account_ids=account_ids,
        source="daily_reconcile",
        actor=actor,
        reason="定时一致性核对发现缺失账号面具",
    )


def _combine_reconcile_results(
    results: list[VoiceProfileGenerationQueueResult],
    manual_required: dict[int, list[int]],
) -> VoiceProfileGenerationReconcileResult:
    return VoiceProfileGenerationReconcileResult(
        tuple(account_id for result in results for account_id in result.created_account_ids),
        tuple(account_id for result in results for account_id in result.existing_account_ids),
        tuple(account_id for result in results for account_id in result.skipped_account_ids),
        tuple(account_id for result in results for account_id in result.manual_required_account_ids)
        + tuple(account_id for account_ids in manual_required.values() for account_id in account_ids),
    )


def refresh_generation_job(session: Session, job: AiAccountVoiceProfileGenerationJob) -> None:
    rows = list(session.scalars(select(AiAccountVoiceProfileGenerationItem).where(
        AiAccountVoiceProfileGenerationItem.job_id == job.id,
    )))
    totals = _job_totals(rows)
    job.total_count = len(rows)
    job.succeeded_count = totals["succeeded"]
    job.retry_wait_count = totals["retry_wait"]
    job.failed_count = totals["manual_required"]
    job.skipped_count = totals["skipped"]
    job.status, job.finished_at = _job_status(rows)


def _refresh_generation_item_job(session: Session, job_id: str) -> None:
    job = session.get(AiAccountVoiceProfileGenerationJob, job_id)
    if job is not None:
        refresh_generation_job(session, job)


def _job_totals(rows: list[AiAccountVoiceProfileGenerationItem]) -> dict[str, int]:
    statuses = [row.status for row in rows]
    return {status: statuses.count(status) for status in ("succeeded", "retry_wait", "manual_required", "skipped")}


def _job_status(rows: list[AiAccountVoiceProfileGenerationItem]) -> tuple[str, object | None]:
    if not rows:
        return "succeeded", _now()
    if all(row.status == "queued" for row in rows):
        return "queued", None
    if any(row.status in OPEN_GENERATION_ITEM_STATUSES for row in rows):
        return "running", None
    if all(row.status in {"succeeded", "skipped"} for row in rows):
        return "succeeded", _now()
    if all(row.status == "cancelled" for row in rows):
        return "cancelled", _now()
    if any(row.status in {"succeeded", "skipped"} for row in rows):
        return "partial", _now()
    return "failed", _now()


def _audit_queue(
    session: Session,
    tenant_id: int,
    actor: str,
    source: str,
    job: AiAccountVoiceProfileGenerationJob,
    created: list[int],
    existing: list[int],
) -> None:
    if not created and not existing:
        return
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action="账号面具生成已入队",
            target_type="ai_account_voice_profile_generation_job",
            target_id=job.id,
            detail=f"source={source};created={created};existing={existing}",
        )
    )


__all__ = [
    "AiAccountVoiceProfileGenerationItem",
    "VoiceProfileGenerationQueueResult",
    "VoiceProfileGenerationReconcileResult",
    "enqueue_voice_profile_generation",
    "reconcile_missing_voice_profile_generation",
    "refresh_generation_job",
    "retry_voice_profile_generation_item",
]
