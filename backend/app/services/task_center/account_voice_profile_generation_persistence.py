from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AiAccountVoiceProfile,
    AiAccountVoiceProfileGenerationAttempt,
    AiAccountVoiceProfileGenerationItem,
    AiAccountVoiceProfileGenerationJob,
    AuditLog,
)

from .account_voice_profile_generation_jobs import refresh_generation_job
from .daily_coverage import release_voice_profile_coverage


GENERIC_SUMMARY_TERMS = ("自然", "随意", "真实", "像真人")


def recover_persisted_generation_item(
    session: Session,
    item: AiAccountVoiceProfileGenerationItem,
    *,
    actor: str,
    timestamp: datetime,
) -> int | None:
    profile = _committed_profile(session, item)
    if profile is None:
        return None
    _mark_item_succeeded(item, profile, timestamp)
    _finish_open_attempt(session, item.id, timestamp)
    release_voice_profile_coverage(
        session,
        tenant_id=item.tenant_id,
        account_id=item.account_id,
        now=timestamp,
    )
    _audit_persisted_recovery(session, item, actor, int(profile.version))
    _refresh_job(session, item.job_id)
    return int(profile.version)


def _committed_profile(
    session: Session,
    item: AiAccountVoiceProfileGenerationItem,
) -> AiAccountVoiceProfile | None:
    profile = session.scalar(
        select(AiAccountVoiceProfile)
        .where(
            AiAccountVoiceProfile.tenant_id == item.tenant_id,
            AiAccountVoiceProfile.account_id == item.account_id,
            AiAccountVoiceProfile.status == "active",
            AiAccountVoiceProfile.quality_status == "active",
            AiAccountVoiceProfile.version >= item.expected_profile_version,
        )
        .order_by(AiAccountVoiceProfile.version.desc())
        .limit(1)
    )
    return profile if profile and _usable_summary(profile.short_prompt_summary) else None


def _usable_summary(value: str | None) -> bool:
    summary = str(value or "").strip()
    return bool(summary) and sum(term in summary for term in GENERIC_SUMMARY_TERMS) < 2


def _mark_item_succeeded(
    item: AiAccountVoiceProfileGenerationItem,
    profile: AiAccountVoiceProfile,
    timestamp: datetime,
) -> None:
    item.status = "succeeded"
    item.result_profile_version = int(profile.version)
    item.error_code = ""
    item.error_detail = ""
    item.next_retry_at = None
    item.finished_at = timestamp
    item.lease_owner = ""
    item.lease_expires_at = None


def _finish_open_attempt(session: Session, item_id: str, timestamp: datetime) -> None:
    attempt = session.scalar(
        select(AiAccountVoiceProfileGenerationAttempt)
        .where(
            AiAccountVoiceProfileGenerationAttempt.item_id == item_id,
            AiAccountVoiceProfileGenerationAttempt.outcome == "running",
        )
        .order_by(AiAccountVoiceProfileGenerationAttempt.attempt_no.desc())
        .limit(1)
    )
    if attempt is None:
        return
    attempt.outcome = "succeeded"
    attempt.error_code = ""
    attempt.error_detail = ""
    attempt.prompt_feedback_summary = "persisted profile recovered"
    attempt.finished_at = timestamp


def _audit_persisted_recovery(
    session: Session,
    item: AiAccountVoiceProfileGenerationItem,
    actor: str,
    version: int,
) -> None:
    session.add(
        AuditLog(
            tenant_id=item.tenant_id,
            actor=actor,
            action="账号面具生成落库确认",
            target_type="ai_account_voice_profile_generation_item",
            target_id=item.id,
            detail=f"account_id={item.account_id};source={item.source};version={version}",
        )
    )


def _refresh_job(session: Session, job_id: str) -> None:
    job = session.get(AiAccountVoiceProfileGenerationJob, job_id)
    if job is not None:
        refresh_generation_job(session, job)


__all__ = ["recover_persisted_generation_item"]
