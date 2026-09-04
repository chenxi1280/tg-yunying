from __future__ import annotations

import json
import socket
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AiAccountVoiceProfile,
    AiAccountVoiceProfileGenerationAttempt,
    AiAccountVoiceProfileGenerationItem,
    AiAccountVoiceProfileGenerationJob,
    AuditLog,
)
from app.services._common import _now
from app.timezone import as_beijing_aware

from .account_voice_profile_generation_jobs import reconcile_missing_voice_profile_generation, refresh_generation_job
from .account_voice_profile_generation import (
    _voice_profile_ai_provider,
    generate_lightweight_voice_profile_payloads,
)
from .account_voice_profile_generation_limits import (
    VoiceProfileProviderLimiterUnavailableError,
    VoiceProfileProviderRateLimitedError,
    VoiceProfileProviderReservation,
    reserve_voice_profile_provider,
)
from .account_voice_profile_generation_persistence import recover_persisted_generation_item
from .account_voice_profiles import ensure_voice_profiles_for_accounts
from .daily_coverage import release_voice_profile_coverage


MAX_AUTO_ATTEMPTS = 4
LEASE_SECONDS = 120
DEFAULT_RECONCILE_INTERVAL_SECONDS = 120
RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30))
TERMINAL_ITEM_STATUSES = {"succeeded", "skipped", "cancelled", "manual_required"}
GenerateOne = Callable[[Session, AiAccountVoiceProfileGenerationItem], int]
ReserveProvider = Callable[[Session, AiAccountVoiceProfileGenerationItem], VoiceProfileProviderReservation]
_last_reconcile_at: datetime | None = None


def drain_voice_profile_generation(
    session_factory,
    *,
    limit: int = 20,
    generate_one: GenerateOne | None = None,
    reserve_provider: ReserveProvider | None = None,
    worker_id: str | None = None,
    reconcile_interval_seconds: float = DEFAULT_RECONCILE_INTERVAL_SECONDS,
    reconcile_limit: int = 100,
) -> int:
    owner = worker_id or f"voice-profile:{socket.gethostname()}"
    processor = generate_one or _generate_one_profile
    provider_reserver = reserve_provider or (_reserve_default_provider if generate_one is None else None)
    _reconcile_missing_profiles_if_due(session_factory, reconcile_limit, reconcile_interval_seconds)
    processed = 0
    for _ in range(max(1, int(limit))):
        claim = _claim_due_item(session_factory, owner)
        if claim is None:
            break
        item_id, recovered = claim
        if not recovered:
            _process_claimed_item(session_factory, item_id, owner, processor, provider_reserver)
        processed += 1
    return processed


def _reconcile_missing_profiles_if_due(session_factory, limit: int, interval_seconds: float) -> None:
    global _last_reconcile_at
    timestamp = _now()
    if not _reconcile_due(timestamp, interval_seconds):
        return
    with session_factory() as session:
        reconcile_missing_voice_profile_generation(
            session,
            limit=max(1, int(limit)),
            actor="voice-profile-worker",
        )
        session.commit()
    _last_reconcile_at = timestamp


def _reconcile_due(timestamp: datetime, interval_seconds: float) -> bool:
    if interval_seconds <= 0 or _last_reconcile_at is None:
        return True
    return timestamp - _last_reconcile_at >= timedelta(seconds=interval_seconds)


def _claim_due_item(session_factory, owner: str) -> tuple[str, bool] | None:
    with session_factory() as session:
        timestamp = _now()
        item = session.scalar(_due_item_statement(session, timestamp))
        if item is None:
            return None
        if item.status == "persist_unknown" and recover_persisted_generation_item(
            session,
            item,
            actor=owner,
            timestamp=timestamp,
        ) is not None:
            session.commit()
            return item.id, True
        _recover_expired_claim(item, timestamp)
        item.status = "generating"
        item.lease_owner = owner
        item.lease_expires_at = timestamp + timedelta(seconds=LEASE_SECONDS)
        job = session.get(AiAccountVoiceProfileGenerationJob, item.job_id)
        if job is not None and job.started_at is None:
            job.started_at = timestamp
        if job is not None:
            refresh_generation_job(session, job)
        session.commit()
        return item.id, False


def _due_item_statement(session: Session, timestamp: datetime):
    due = or_(
        AiAccountVoiceProfileGenerationItem.status == "queued",
        and_(
            AiAccountVoiceProfileGenerationItem.status == "retry_wait",
            AiAccountVoiceProfileGenerationItem.next_retry_at <= timestamp,
        ),
        and_(
            AiAccountVoiceProfileGenerationItem.status == "generating",
            AiAccountVoiceProfileGenerationItem.lease_expires_at <= timestamp,
        ),
        and_(
            AiAccountVoiceProfileGenerationItem.status == "persist_unknown",
            AiAccountVoiceProfileGenerationItem.next_retry_at <= timestamp,
        ),
    )
    statement = select(AiAccountVoiceProfileGenerationItem).where(due).order_by(
        AiAccountVoiceProfileGenerationItem.next_retry_at.asc().nullsfirst(),
        AiAccountVoiceProfileGenerationItem.created_at.asc(),
        AiAccountVoiceProfileGenerationItem.id.asc(),
    ).limit(1)
    if session.bind is not None and session.bind.dialect.name != "sqlite":
        return statement.with_for_update(skip_locked=True)
    return statement


def _recover_expired_claim(item: AiAccountVoiceProfileGenerationItem, timestamp: datetime) -> None:
    if item.status == "persist_unknown" or (
        item.status == "generating"
        and item.lease_expires_at
        and as_beijing_aware(item.lease_expires_at) <= as_beijing_aware(timestamp)
    ):
        item.status = "queued"
        item.lease_owner = ""
        item.lease_expires_at = None


def _process_claimed_item(
    session_factory,
    item_id: str,
    owner: str,
    generate_one: GenerateOne,
    reserve_provider: ReserveProvider | None,
) -> None:
    try:
        reservation = _reserve_provider_execution(session_factory, item_id, owner, reserve_provider)
    except VoiceProfileProviderRateLimitedError as exc:
        _defer_provider_rate_limit(session_factory, item_id, owner, exc)
        return
    except Exception as exc:  # noqa: BLE001 - provider selection and limiter availability are durable failures.
        attempt_id = _start_attempt(session_factory, item_id, owner, _error_provider(exc))
        if attempt_id is not None:
            _complete_failure(session_factory, item_id, attempt_id, owner, exc)
        return
    attempt_id = _start_attempt(session_factory, item_id, owner, reservation.provider if reservation else "")
    if attempt_id is None:
        if reservation is not None:
            reservation.release()
        return
    try:
        result_version = _invoke_generator(session_factory, item_id, owner, generate_one)
    except Exception as exc:  # noqa: BLE001 - every provider failure must become a durable item state.
        _complete_failure(session_factory, item_id, attempt_id, owner, exc)
    else:
        _complete_success(session_factory, item_id, attempt_id, owner, result_version)
    finally:
        if reservation is not None:
            reservation.release()


def _reserve_provider_execution(
    session_factory,
    item_id: str,
    owner: str,
    reserve_provider: ReserveProvider | None,
) -> VoiceProfileProviderReservation | None:
    if reserve_provider is None:
        return None
    with session_factory() as session:
        item = session.get(AiAccountVoiceProfileGenerationItem, item_id)
        if not _owned_generating_item(item, owner):
            raise RuntimeError("voice profile generation lease was lost")
        return reserve_provider(session, item)


def _reserve_default_provider(
    session: Session,
    item: AiAccountVoiceProfileGenerationItem,
) -> VoiceProfileProviderReservation:
    provider, _setting = _voice_profile_ai_provider(session, item.tenant_id)
    return reserve_voice_profile_provider(tenant_id=item.tenant_id, provider_id=provider.id)


def _start_attempt(session_factory, item_id: str, owner: str, provider: str = "") -> str | None:
    with session_factory() as session:
        item = session.get(AiAccountVoiceProfileGenerationItem, item_id)
        if not _owned_generating_item(item, owner):
            return None
        item.attempt_count = int(item.attempt_count or 0) + 1
        attempt = AiAccountVoiceProfileGenerationAttempt(
            tenant_id=item.tenant_id,
            job_id=item.job_id,
            item_id=item.id,
            attempt_no=item.attempt_count,
            stage="generate",
            provider=provider,
        )
        session.add(attempt)
        session.commit()
        return attempt.id


def _defer_provider_rate_limit(
    session_factory,
    item_id: str,
    owner: str,
    error: VoiceProfileProviderRateLimitedError,
) -> None:
    with session_factory() as session:
        item = session.get(AiAccountVoiceProfileGenerationItem, item_id)
        if not _owned_generating_item(item, owner):
            return
        timestamp = _now()
        item.status = "retry_wait"
        item.error_code = "voice_profile_provider_rate_limited"
        item.error_detail = str(error)[:500]
        item.next_retry_at = timestamp + timedelta(seconds=error.wait_seconds)
        item.lease_owner = ""
        item.lease_expires_at = None
        _audit_generation_outcome(
            session,
            item,
            actor=owner,
            action="账号面具生成待重试",
            detail=_failure_audit_detail(item, item.error_code, error),
        )
        _refresh_parent_job(session, item.job_id)
        session.commit()


def _error_provider(error: Exception) -> str:
    if isinstance(error, (VoiceProfileProviderRateLimitedError, VoiceProfileProviderLimiterUnavailableError)):
        return error.provider
    return ""


def _invoke_generator(
    session_factory,
    item_id: str,
    owner: str,
    generate_one: GenerateOne,
) -> int:
    with session_factory() as session:
        item = session.get(AiAccountVoiceProfileGenerationItem, item_id)
        if not _owned_generating_item(item, owner):
            raise RuntimeError("voice profile generation lease was lost")
        try:
            result_version = generate_one(session, item)
            item.status = "persist_unknown"
            item.next_retry_at = _now()
            session.commit()
            return int(result_version)
        except Exception:
            session.rollback()
            raise


def _complete_success(
    session_factory,
    item_id: str,
    attempt_id: str,
    owner: str,
    result_version: int,
) -> None:
    with session_factory() as session:
        item = session.get(AiAccountVoiceProfileGenerationItem, item_id)
        if not _owned_generating_item(item, owner):
            return
        timestamp = _now()
        item.status = "succeeded"
        item.result_profile_version = result_version
        item.error_code = ""
        item.error_detail = ""
        item.next_retry_at = None
        item.finished_at = timestamp
        item.lease_owner = ""
        item.lease_expires_at = None
        _finish_attempt(session, attempt_id, "succeeded", timestamp)
        release_voice_profile_coverage(
            session,
            tenant_id=item.tenant_id,
            account_id=item.account_id,
            now=timestamp,
        )
        _audit_generation_outcome(
            session,
            item,
            actor=owner,
            action="账号面具生成成功",
            detail=f"account_id={item.account_id};source={item.source};version={result_version}",
        )
        _refresh_parent_job(session, item.job_id)
        session.commit()


def _complete_failure(
    session_factory,
    item_id: str,
    attempt_id: str,
    owner: str,
    error: Exception,
) -> None:
    with session_factory() as session:
        item = session.get(AiAccountVoiceProfileGenerationItem, item_id)
        if not _owned_generating_item(item, owner):
            return
        timestamp = _now()
        error_code = _error_code(error)
        next_status, next_retry_at = _retry_state(item.attempt_count, timestamp, error_code)
        item.status = next_status
        item.error_code = error_code
        item.error_detail = str(error)[:500]
        item.next_retry_at = next_retry_at
        item.finished_at = timestamp if next_status in TERMINAL_ITEM_STATUSES else None
        item.lease_owner = ""
        item.lease_expires_at = None
        _finish_attempt(session, attempt_id, next_status, timestamp, error_code, str(error)[:500])
        action = _failure_audit_action(next_status)
        _audit_generation_outcome(
            session,
            item,
            actor=owner,
            action=action,
            detail=_failure_audit_detail(item, error_code, error),
        )
        _refresh_parent_job(session, item.job_id)
        session.commit()


def _owned_generating_item(item: AiAccountVoiceProfileGenerationItem | None, owner: str) -> bool:
    return bool(item and item.status in {"generating", "persist_unknown"} and item.lease_owner == owner)


def _finish_attempt(
    session: Session,
    attempt_id: str,
    outcome: str,
    timestamp: datetime,
    error_code: str = "",
    error_detail: str = "",
) -> None:
    attempt = session.get(AiAccountVoiceProfileGenerationAttempt, attempt_id)
    if attempt is None:
        raise RuntimeError(f"voice profile generation attempt not found: {attempt_id}")
    attempt.outcome = outcome
    attempt.error_code = error_code
    attempt.error_detail = error_detail
    attempt.prompt_feedback_summary = _prompt_feedback_summary(error_code, error_detail)
    attempt.finished_at = timestamp


def _audit_generation_outcome(
    session: Session,
    item: AiAccountVoiceProfileGenerationItem,
    *,
    actor: str,
    action: str,
    detail: str,
) -> None:
    session.add(
        AuditLog(
            tenant_id=item.tenant_id,
            actor=actor,
            action=action,
            target_type="ai_account_voice_profile_generation_item",
            target_id=item.id,
            detail=detail,
        )
    )


def _failure_audit_detail(item: AiAccountVoiceProfileGenerationItem, error_code: str, error: Exception) -> str:
    detail = str(error).replace("\n", " ").strip()[:500]
    return f"account_id={item.account_id};source={item.source};error_code={error_code};detail={detail}"


def _failure_audit_action(status: str) -> str:
    if status == "manual_required":
        return "账号面具生成需要人工处理"
    if status == "skipped":
        return "账号面具生成已跳过"
    return "账号面具生成待重试"


def _prompt_feedback_summary(error_code: str, error_detail: str) -> str:
    detail = error_detail.replace("\n", " ").strip()[:500]
    return f"{error_code}: {detail}" if detail else error_code


def _refresh_parent_job(session: Session, job_id: str) -> None:
    job = session.get(AiAccountVoiceProfileGenerationJob, job_id)
    if job is not None:
        refresh_generation_job(session, job)


def _retry_state(attempt_count: int, timestamp: datetime, error_code: str) -> tuple[str, datetime | None]:
    if error_code == "voice_profile_account_ineligible":
        return "skipped", None
    if error_code == "voice_profile_provider_config_invalid" or attempt_count >= MAX_AUTO_ATTEMPTS:
        return "manual_required", None
    index = min(max(0, attempt_count - 1), len(RETRY_DELAYS) - 1)
    return "retry_wait", timestamp + RETRY_DELAYS[index]


def _error_code(error: Exception) -> str:
    if isinstance(error, VoiceProfileProviderRateLimitedError):
        return "voice_profile_provider_rate_limited"
    if isinstance(error, VoiceProfileProviderLimiterUnavailableError):
        return "voice_profile_provider_unavailable"
    if isinstance(error, json.JSONDecodeError):
        return "voice_profile_output_malformed"
    message = str(error).lower()
    if "http 429" in message or "rate_limit_error" in message or "速率限制" in message:
        return "voice_profile_provider_rate_limited"
    if "json" in message or "输出行" in message or "输出为空" in message:
        return "voice_profile_output_malformed"
    if "缺少字段" in message or "summary missing" in message:
        return "voice_profile_output_incomplete"
    if "too generic" in message:
        return "voice_profile_too_generic"
    if "too similar" in message:
        return "voice_profile_similarity_rejected"
    if "gender" in message or "male" in message:
        return "voice_profile_identity_invalid"
    if isinstance(error, TimeoutError):
        return "voice_profile_provider_timeout"
    if "timed out" in message or "timeout" in message:
        return "voice_profile_provider_timeout"
    if "account_action_not_allowed" in message or "account_purpose_mismatch" in message:
        return "voice_profile_account_ineligible"
    if "供应商" in message or "provider" in message:
        return "voice_profile_provider_config_invalid"
    return "voice_profile_provider_unavailable"


def _generate_one_profile(session: Session, item: AiAccountVoiceProfileGenerationItem) -> int:
    def generator(account_ids: list[int]) -> list[dict]:
        return generate_lightweight_voice_profile_payloads(
            session,
            tenant_id=item.tenant_id,
            account_ids=account_ids,
        )

    ensure_voice_profiles_for_accounts(
        session,
        tenant_id=item.tenant_id,
        account_ids=[item.account_id],
        generator=generator,
    )
    profile = session.scalar(
        select(AiAccountVoiceProfile)
        .where(
            AiAccountVoiceProfile.tenant_id == item.tenant_id,
            AiAccountVoiceProfile.account_id == item.account_id,
            AiAccountVoiceProfile.status == "active",
            AiAccountVoiceProfile.quality_status == "active",
        )
        .order_by(AiAccountVoiceProfile.version.desc())
        .limit(1)
    )
    if profile is None or not str(profile.short_prompt_summary or "").strip():
        raise RuntimeError("voice profile generation did not create an active profile")
    return int(profile.version)


__all__ = ["drain_voice_profile_generation"]
