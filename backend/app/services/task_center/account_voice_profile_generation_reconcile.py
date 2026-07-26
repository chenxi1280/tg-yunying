from __future__ import annotations

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models import (
    AiAccountVoiceProfile,
    AiAccountVoiceProfileGenerationItem,
    AccountStatus,
    OPEN_GENERATION_ITEM_STATUSES,
    TgAccount,
)
from app.services.account_usage_policy import apply_operational_account_filters


GENERIC_SUMMARY_TERMS = ("自然", "随意", "真实", "像真人")
PRECHECK_SAMPLE_LIMIT = 5
PRECHECK_GENERATION_STATUSES = (*OPEN_GENERATION_ITEM_STATUSES, "manual_required")


def missing_profile_accounts(
    session: Session,
    tenant_id: int | None,
    limit: int,
) -> dict[int, list[int]]:
    statement = _missing_profile_statement(tenant_id).where(
        ~_open_item_exists(),
        ~_manual_required_item_exists(),
    )
    return _account_groups(session, statement, limit)


def manual_required_missing_profile_accounts(
    session: Session,
    tenant_id: int | None,
    limit: int,
) -> dict[int, list[int]]:
    statement = _missing_profile_statement(tenant_id).where(_manual_required_item_exists())
    return _account_groups(session, statement, limit)


def voice_profile_precheck_summary(
    session: Session,
    *,
    tenant_id: int,
    account_ids: list[int],
) -> dict[str, object]:
    unique_ids = tuple(dict.fromkeys(int(account_id) for account_id in account_ids))
    counts = _precheck_counts()
    samples = _precheck_samples()
    profiles = _latest_profiles(session, tenant_id, unique_ids)
    item_statuses = _latest_generation_statuses(session, tenant_id, unique_ids)
    for account_id in unique_ids:
        state = _precheck_account_state(profiles.get(account_id), item_statuses.get(account_id))
        counts[f"{state}_account_count"] += 1
        if state in samples and len(samples[state]) < PRECHECK_SAMPLE_LIMIT:
            samples[state].append(account_id)
    return {"target_account_count": len(unique_ids), **counts, "samples": samples}


def _precheck_counts() -> dict[str, int]:
    return {
        "usable_account_count": 0,
        "queued_account_count": 0,
        "retry_wait_account_count": 0,
        "manual_required_account_count": 0,
        "disabled_account_count": 0,
        "missing_account_count": 0,
    }


def _precheck_samples() -> dict[str, list[int]]:
    return {"queued": [], "retry_wait": [], "manual_required": [], "disabled": [], "missing": []}


def _latest_profiles(session: Session, tenant_id: int, account_ids: tuple[int, ...]) -> dict[int, AiAccountVoiceProfile]:
    if not account_ids:
        return {}
    rows = session.scalars(
        select(AiAccountVoiceProfile)
        .where(AiAccountVoiceProfile.tenant_id == tenant_id, AiAccountVoiceProfile.account_id.in_(account_ids))
        .order_by(AiAccountVoiceProfile.account_id.asc(), AiAccountVoiceProfile.version.desc(), AiAccountVoiceProfile.id.desc())
    )
    profiles: dict[int, AiAccountVoiceProfile] = {}
    for profile in rows:
        profiles.setdefault(int(profile.account_id), profile)
    return profiles


def _latest_generation_statuses(session: Session, tenant_id: int, account_ids: tuple[int, ...]) -> dict[int, str]:
    if not account_ids:
        return {}
    rows = session.scalars(
        select(AiAccountVoiceProfileGenerationItem)
        .where(
            AiAccountVoiceProfileGenerationItem.tenant_id == tenant_id,
            AiAccountVoiceProfileGenerationItem.account_id.in_(account_ids),
            AiAccountVoiceProfileGenerationItem.status.in_(PRECHECK_GENERATION_STATUSES),
        )
        .order_by(AiAccountVoiceProfileGenerationItem.updated_at.desc(), AiAccountVoiceProfileGenerationItem.id.desc())
    )
    statuses: dict[int, str] = {}
    for item in rows:
        account_id = int(item.account_id)
        current = statuses.get(account_id)
        if current is None or _generation_status_priority(item.status) > _generation_status_priority(current):
            statuses[account_id] = item.status
    return statuses


def _generation_status_priority(status: str) -> int:
    return 2 if status in OPEN_GENERATION_ITEM_STATUSES else 1


def _precheck_account_state(profile: AiAccountVoiceProfile | None, generation_status: str | None) -> str:
    if profile is not None and profile.status == "disabled":
        return "disabled"
    if _profile_is_usable(profile):
        return "usable"
    if generation_status == "retry_wait":
        return "retry_wait"
    if generation_status in OPEN_GENERATION_ITEM_STATUSES:
        return "queued"
    if generation_status == "manual_required":
        return "manual_required"
    return "missing"


def _profile_is_usable(profile: AiAccountVoiceProfile | None) -> bool:
    if profile is None or profile.status != "active" or profile.quality_status != "active" or int(profile.version or 0) < 1:
        return False
    summary = str(profile.short_prompt_summary or "").strip()
    return bool(summary) and sum(term in summary for term in GENERIC_SUMMARY_TERMS) < 2


def _missing_profile_statement(tenant_id: int | None):
    latest = aliased(AiAccountVoiceProfile)
    generic_count = sum(
        case((latest.short_prompt_summary.contains(term), 1), else_=0)
        for term in GENERIC_SUMMARY_TERMS
    )
    unusable = or_(
        latest.status != "active",
        latest.quality_status != "active",
        latest.version < 1,
        func.trim(latest.short_prompt_summary) == "",
        generic_count >= 2,
    )
    statement = select(TgAccount.tenant_id, TgAccount.id).outerjoin(
        latest, latest.id == _latest_profile_id()
    ).where(
        TgAccount.deleted_at.is_(None),
        TgAccount.status == AccountStatus.ACTIVE.value,
        or_(latest.id.is_(None), and_(latest.status != "disabled", unusable)),
    )
    return statement.where(TgAccount.tenant_id == tenant_id) if tenant_id is not None else statement


def _latest_profile_id():
    return (
        select(AiAccountVoiceProfile.id)
        .where(
            AiAccountVoiceProfile.tenant_id == TgAccount.tenant_id,
            AiAccountVoiceProfile.account_id == TgAccount.id,
        )
        .order_by(AiAccountVoiceProfile.version.desc(), AiAccountVoiceProfile.id.desc())
        .limit(1)
        .correlate(TgAccount)
        .scalar_subquery()
    )


def _open_item_exists():
    return select(AiAccountVoiceProfileGenerationItem.id).where(
        AiAccountVoiceProfileGenerationItem.tenant_id == TgAccount.tenant_id,
        AiAccountVoiceProfileGenerationItem.account_id == TgAccount.id,
        AiAccountVoiceProfileGenerationItem.status.in_(OPEN_GENERATION_ITEM_STATUSES),
    ).exists()


def _manual_required_item_exists():
    return select(AiAccountVoiceProfileGenerationItem.id).where(
        AiAccountVoiceProfileGenerationItem.tenant_id == TgAccount.tenant_id,
        AiAccountVoiceProfileGenerationItem.account_id == TgAccount.id,
        AiAccountVoiceProfileGenerationItem.status == "manual_required",
    ).exists()


def _account_groups(session: Session, statement, limit: int) -> dict[int, list[int]]:
    rows = session.execute(
        apply_operational_account_filters(statement)
        .order_by(TgAccount.id.asc())
        .limit(max(1, int(limit)))
    ).all()
    result: dict[int, list[int]] = {}
    for item_tenant_id, account_id in rows:
        result.setdefault(int(item_tenant_id), []).append(int(account_id))
    return result
