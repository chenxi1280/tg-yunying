from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import AiAccountVoiceProfile, AuditLog
from app.services.task_center.account_voice_profile_cache import refresh_voice_profile_cache

VOICE_PROFILE_MUTABLE_STATUSES = {"active", "disabled"}


def batch_update_voice_profile_status(
    session: Session,
    *,
    tenant_id: int,
    account_ids: list[int],
    status: str,
    actor: str,
) -> dict[str, int]:
    target_status = status.strip()
    if target_status not in VOICE_PROFILE_MUTABLE_STATUSES:
        raise ValueError(f"unsupported voice profile status: {status}")
    updated = 0
    for account_id in _unique_account_ids(account_ids):
        profile = _latest_profile(session, tenant_id, account_id)
        if not profile or profile.status == target_status:
            continue
        profile.status = target_status
        profile.updated_by = actor
        _audit_status_change(session, tenant_id, actor, account_id, target_status)
        refresh_voice_profile_cache(profile)
        updated += 1
    session.flush()
    return {"updated": updated, "skipped": max(0, len(_unique_account_ids(account_ids)) - updated)}


def _unique_account_ids(account_ids: list[int]) -> list[int]:
    return list(dict.fromkeys(int(account_id) for account_id in account_ids))


def _latest_profile(session: Session, tenant_id: int, account_id: int) -> AiAccountVoiceProfile | None:
    return session.scalar(
        select(AiAccountVoiceProfile)
        .where(AiAccountVoiceProfile.tenant_id == tenant_id, AiAccountVoiceProfile.account_id == account_id)
        .order_by(desc(AiAccountVoiceProfile.version))
        .limit(1)
    )


def _audit_status_change(session: Session, tenant_id: int, actor: str, account_id: int, status: str) -> None:
    action = "批量恢复账号面具" if status == "active" else "批量停用账号面具"
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            target_type="ai_account_voice_profile",
            target_id=str(account_id),
            detail=f"status={status}",
        )
    )


__all__ = ["batch_update_voice_profile_status"]
