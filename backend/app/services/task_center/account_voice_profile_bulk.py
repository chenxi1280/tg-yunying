from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import AiAccountVoiceProfile, AuditLog
from app.services.task_center.account_voice_profile_cache import refresh_voice_profile_cache
from app.services.task_center.account_voice_profile_usage import voice_profile_allowed_ids

VOICE_PROFILE_MUTABLE_STATUSES = {"active", "disabled"}


def batch_update_voice_profile_status(
    session: Session,
    *,
    tenant_id: int,
    account_ids: list[int],
    status: str,
    actor: str,
) -> dict[str, object]:
    target_status = status.strip()
    if target_status not in VOICE_PROFILE_MUTABLE_STATUSES:
        raise ValueError(f"unsupported voice profile status: {status}")
    updated = 0
    unique_ids = _unique_account_ids(account_ids)
    allowed_ids, usage_errors = voice_profile_allowed_ids(session, tenant_id, unique_ids)
    items: list[dict[str, object]] = []
    for account_id in unique_ids:
        if account_id in usage_errors:
            items.append(_result_item(account_id, "skipped", usage_errors[account_id]))
            continue
        if account_id not in allowed_ids:
            items.append(_result_item(account_id, "skipped", ""))
            continue
        profile = _latest_profile(session, tenant_id, account_id)
        if not profile or profile.status == target_status:
            items.append(_result_item(account_id, "skipped", ""))
            continue
        profile.status = target_status
        profile.updated_by = actor
        _audit_status_change(session, tenant_id, actor, account_id, target_status)
        refresh_voice_profile_cache(profile)
        items.append(_result_item(account_id, "updated", ""))
        updated += 1
    session.flush()
    result: dict[str, object] = {"updated": updated, "skipped": max(0, len(unique_ids) - updated)}
    if usage_errors:
        result["items"] = items
    return result


def _result_item(account_id: int, status: str, skipped_reason: str) -> dict[str, object]:
    return {"account_id": account_id, "status": status, "skipped_reason": skipped_reason}


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
