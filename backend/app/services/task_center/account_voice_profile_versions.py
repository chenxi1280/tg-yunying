from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import AiAccountVoiceProfile, AuditLog, TgAccount
from app.services._common import _now
from app.services.task_center.account_voice_profile_cache import refresh_voice_profile_cache


PROFILE_COPY_FIELDS = (
    "age_band",
    "persona_experiences",
    "consumption_experiences",
    "sentence_length",
    "interaction_habits",
    "tone_strength",
    "lexical_preferences",
    "emoji_policy",
    "forbidden_expressions",
    "short_prompt_summary",
    "similarity_score",
    "quality_status",
)


def list_voice_profile_versions(session: Session, *, tenant_id: int, account_id: int) -> list[dict[str, Any]]:
    _require_account(session, tenant_id, account_id)
    rows = session.scalars(
        select(AiAccountVoiceProfile)
        .where(AiAccountVoiceProfile.tenant_id == tenant_id, AiAccountVoiceProfile.account_id == account_id)
        .order_by(desc(AiAccountVoiceProfile.version))
    ).all()
    return [_version_projection(row) for row in rows]


def list_voice_profile_audits(session: Session, *, tenant_id: int, account_id: int) -> list[dict[str, Any]]:
    _require_account(session, tenant_id, account_id)
    rows = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.target_type == "ai_account_voice_profile",
            AuditLog.target_id == str(account_id),
        )
        .order_by(desc(AuditLog.created_at), desc(AuditLog.id))
    ).all()
    return [_audit_projection(row) for row in rows]


def rollback_voice_profile(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    source_version: int,
    actor: str,
) -> AiAccountVoiceProfile:
    _require_account(session, tenant_id, account_id)
    source = _profile_by_version(session, tenant_id, account_id, source_version)
    current = _latest_profile(session, tenant_id, account_id)
    restored = _copy_profile_for_rollback(source, current, actor)
    if current and current.status == "active":
        current.status = "superseded"
    session.add(restored)
    session.add(_rollback_audit(tenant_id, actor, account_id, source_version, restored.version))
    session.flush()
    refresh_voice_profile_cache(restored)
    return restored


def _require_account(session: Session, tenant_id: int, account_id: int) -> TgAccount:
    account = session.scalar(select(TgAccount).where(TgAccount.tenant_id == tenant_id, TgAccount.id == account_id))
    if not account:
        raise ValueError("account not found")
    return account


def _profile_by_version(
    session: Session,
    tenant_id: int,
    account_id: int,
    source_version: int,
) -> AiAccountVoiceProfile:
    profile = session.scalar(
        select(AiAccountVoiceProfile).where(
            AiAccountVoiceProfile.tenant_id == tenant_id,
            AiAccountVoiceProfile.account_id == account_id,
            AiAccountVoiceProfile.version == source_version,
        )
    )
    if not profile:
        raise ValueError("voice profile version not found")
    return profile


def _latest_profile(session: Session, tenant_id: int, account_id: int) -> AiAccountVoiceProfile | None:
    return session.scalar(
        select(AiAccountVoiceProfile)
        .where(AiAccountVoiceProfile.tenant_id == tenant_id, AiAccountVoiceProfile.account_id == account_id)
        .order_by(desc(AiAccountVoiceProfile.version))
        .limit(1)
    )


def _copy_profile_for_rollback(
    source: AiAccountVoiceProfile,
    current: AiAccountVoiceProfile | None,
    actor: str,
) -> AiAccountVoiceProfile:
    data = {field: _copy_value(getattr(source, field)) for field in PROFILE_COPY_FIELDS}
    return AiAccountVoiceProfile(
        tenant_id=source.tenant_id,
        account_id=source.account_id,
        version=int(current.version if current else 0) + 1,
        source="rollback",
        status="active",
        last_rebuilt_at=_now(),
        updated_by=actor,
        **data,
    )


def _copy_value(value: Any) -> Any:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def _rollback_audit(
    tenant_id: int,
    actor: str,
    account_id: int,
    source_version: int,
    target_version: int,
) -> AuditLog:
    return AuditLog(
        tenant_id=tenant_id,
        actor=actor,
        action="回滚账号表达卡",
        target_type="ai_account_voice_profile",
        target_id=str(account_id),
        detail=f"source_version={source_version},target_version={target_version}",
    )


def _version_projection(profile: AiAccountVoiceProfile) -> dict[str, Any]:
    return {
        "version": profile.version,
        "status": profile.status,
        "source": profile.source,
        "age_band": profile.age_band,
        "sentence_length": profile.sentence_length,
        "tone_strength": profile.tone_strength,
        "emoji_policy": profile.emoji_policy,
        "short_prompt_summary": profile.short_prompt_summary,
        "quality_status": profile.quality_status,
        "similarity_score": profile.similarity_score,
        "updated_by": profile.updated_by,
        "updated_at": profile.updated_at,
    }


def _audit_projection(row: AuditLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "actor": row.actor,
        "action": row.action,
        "detail": row.detail,
        "created_at": row.created_at,
    }


__all__ = ["list_voice_profile_audits", "list_voice_profile_versions", "rollback_voice_profile"]
