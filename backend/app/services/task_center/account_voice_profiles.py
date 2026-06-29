from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountStatus, AiAccountVoiceProfile, AuditLog, TgAccount
from app.services._common import _now
from app.services.ai_config import ai_provider_credentials
from app.services.task_center.account_stance_memory import group_stance_summaries, upsert_group_stance_memory
from app.services.task_center.account_voice_profile_cache import (
    cached_voice_profile_prompt_details,
    refresh_voice_profile_cache,
    refresh_voice_profile_cache_many,
)
from app.services.task_center.account_voice_profile_generation import (
    VOICE_PROFILE_INITIAL_MAX_TOKENS,
    VOICE_PROFILE_RETRY_MAX_TOKENS,
    _generate_voice_profile_payloads,
    _parse_voice_profile_payloads,
    _valid_summary,
    _validate_summary,
    _voice_profile_ai_provider,
)
from app.services.task_center.account_voice_profile_quality import generate_diverse_voice_profile_batch
from app.services.task_center.account_voice_profile_search import filter_voice_profile_rows

VOICE_PROFILE_BATCH_SIZE = 2
EDITABLE_PROFILE_FIELDS = {
    "age_band", "persona_experiences", "consumption_experiences", "sentence_length", "interaction_habits",
    "tone_strength", "lexical_preferences", "emoji_policy", "forbidden_expressions", "short_prompt_summary",
    "status", "quality_status",
}

def list_voice_profiles(
    session: Session,
    *,
    tenant_id: int,
    search: str = "",
    profile_status: str = "",
) -> list[dict[str, Any]]:
    accounts = _search_accounts(session, tenant_id, "")
    latest = _latest_profiles(session, tenant_id, [account.id for account in accounts])
    rows = [_profile_projection(account, latest.get(account.id)) for account in accounts]
    rows = filter_voice_profile_rows(rows, search)
    if profile_status:
        rows = [row for row in rows if row["profile_status"] == profile_status]
    return rows


def patch_voice_profile(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    patch: dict[str, Any],
    actor: str,
) -> AiAccountVoiceProfile:
    account = _require_account(session, tenant_id, account_id)
    current = _latest_profile(session, tenant_id, account_id)
    next_profile = _patched_profile(tenant_id, account.id, current, patch, actor)
    _validate_summary(next_profile.short_prompt_summary, account_id)
    if current and current.status == "active":
        current.status = "superseded"
    session.add(next_profile)
    _audit(session, tenant_id, actor, "编辑账号表达卡", account_id, f"version={next_profile.version}")
    session.flush()
    refresh_voice_profile_cache(next_profile)
    return next_profile


def rebuild_voice_profile(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    generator: Callable[[list[int]], list[dict[str, Any]]],
    actor: str,
) -> AiAccountVoiceProfile:
    _require_account(session, tenant_id, account_id)
    profile = _generated_profile(session, tenant_id, account_id, generator)
    current = _latest_profile(session, tenant_id, account_id)
    profile.version = int(current.version if current else 0) + 1
    if current and current.status == "active":
        current.status = "superseded"
    session.add(profile)
    _audit(session, tenant_id, actor, "重建账号表达卡", account_id, f"version={profile.version}")
    session.flush()
    refresh_voice_profile_cache(profile)
    return profile


def batch_rebuild_voice_profiles(
    session: Session,
    *,
    tenant_id: int,
    account_ids: list[int],
    generator: Callable[[list[int]], list[dict[str, Any]]],
    actor: str,
    missing_only: bool = False,
) -> dict[str, int]:
    candidate_ids = _batch_candidate_account_ids(session, tenant_id, account_ids, missing_only)
    target_ids = _missing_account_ids(session, tenant_id, candidate_ids) if missing_only else candidate_ids
    if not target_ids:
        return {"created": 0, "skipped": len(candidate_ids)}
    created = _batch_insert_generated(session, tenant_id, target_ids, generator, actor)
    return {"created": created, "skipped": max(0, len(candidate_ids) - created)}


def generate_voice_profiles_with_ai(session: Session, *, tenant_id: int) -> Callable[[list[int]], list[dict[str, Any]]]:
    provider, setting = _voice_profile_ai_provider(session, tenant_id)
    credentials = ai_provider_credentials(provider)
    if credentials.base_url.startswith("mock://"):
        raise RuntimeError("账号表达卡重建需要真实 AI 供应商，当前供应商为 mock")

    def _generator(account_ids: list[int]) -> list[dict[str, Any]]:
        return _generate_voice_profile_payloads(session, tenant_id, account_ids, credentials, setting)

    return _generator


def ensure_voice_profiles_for_accounts(
    session: Session,
    *,
    tenant_id: int,
    account_ids: list[int],
    generator: Callable[[list[int]], list[dict[str, Any]]] | None,
) -> int:
    missing = _missing_account_ids(session, tenant_id, account_ids)
    if not missing:
        return 0
    if generator is None:
        raise RuntimeError("voice profile generator is required")
    return _batch_insert_generated(session, tenant_id, missing, generator, actor="system")


def voice_profile_prompt_summaries(
    session: Session,
    *,
    tenant_id: int,
    account_ids: list[int],
) -> dict[int, str]:
    details = voice_profile_prompt_details(session, tenant_id=tenant_id, account_ids=account_ids)
    return {account_id: str(item["summary"]) for account_id, item in details.items()}


def voice_profile_prompt_details(
    session: Session,
    *,
    tenant_id: int,
    account_ids: list[int],
) -> dict[int, dict[str, str | int]]:
    cached, missed_ids = cached_voice_profile_prompt_details(tenant_id, account_ids)
    if not missed_ids:
        return cached
    rows = session.scalars(
        select(AiAccountVoiceProfile).where(
            AiAccountVoiceProfile.tenant_id == tenant_id,
            AiAccountVoiceProfile.account_id.in_(missed_ids),
            AiAccountVoiceProfile.status == "active",
            AiAccountVoiceProfile.quality_status == "active",
        )
    )
    result: dict[int, dict[str, str | int]] = dict(cached)
    backfill_rows: list[AiAccountVoiceProfile] = []
    for row in rows:
        current = result.get(row.account_id)
        if current and int(current["version"]) >= int(row.version or 0):
            continue
        result[row.account_id] = {"version": int(row.version or 0), "summary": row.short_prompt_summary}
        backfill_rows.append(row)
    refresh_voice_profile_cache_many(backfill_rows)
    return result


def _missing_account_ids(session: Session, tenant_id: int, account_ids: list[int]) -> list[int]:
    unique_ids = list(dict.fromkeys(int(account_id) for account_id in account_ids))
    existing = set(
        session.scalars(
            select(AiAccountVoiceProfile.account_id).where(
                AiAccountVoiceProfile.tenant_id == tenant_id,
                AiAccountVoiceProfile.account_id.in_(unique_ids),
                AiAccountVoiceProfile.status == "active",
            )
        )
    )
    return [account_id for account_id in unique_ids if account_id not in existing]


def _batch_insert_generated(
    session: Session,
    tenant_id: int,
    account_ids: list[int],
    generator: Callable[[list[int]], list[dict[str, Any]]],
    actor: str,
) -> int:
    created = 0
    for chunk in _chunked_account_ids(account_ids):
        profiles, diversity_scores = generate_diverse_voice_profile_batch(generator, chunk)
        chunk_rows: list[AiAccountVoiceProfile] = []
        for account_id in chunk:
            profile = profiles.get(account_id)
            row = _profile_from_generated(tenant_id, account_id, profile, _valid_summary(profile, account_id))
            row.similarity_score = diversity_scores.get(account_id)
            session.add(row)
            chunk_rows.append(row)
            _audit(session, tenant_id, actor, "批量生成账号表达卡", account_id, f"version={row.version}")
            created += 1
        session.flush()
        refresh_voice_profile_cache_many(chunk_rows)
    return created


def _chunked_account_ids(account_ids: list[int]) -> list[list[int]]:
    return [account_ids[index:index + VOICE_PROFILE_BATCH_SIZE] for index in range(0, len(account_ids), VOICE_PROFILE_BATCH_SIZE)]


def _search_accounts(session: Session, tenant_id: int, search: str) -> list[TgAccount]:
    stmt = select(TgAccount).where(TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None))
    keyword = search.strip()
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(or_(TgAccount.display_name.ilike(like), TgAccount.username.ilike(like), TgAccount.phone_masked.ilike(like)))
    return list(session.scalars(stmt.order_by(TgAccount.id.asc())))


def _latest_profiles(session: Session, tenant_id: int, account_ids: list[int]) -> dict[int, AiAccountVoiceProfile]:
    if not account_ids:
        return {}
    rows = session.scalars(
        select(AiAccountVoiceProfile)
        .where(AiAccountVoiceProfile.tenant_id == tenant_id, AiAccountVoiceProfile.account_id.in_(account_ids))
        .order_by(AiAccountVoiceProfile.account_id.asc(), AiAccountVoiceProfile.version.desc())
    )
    result: dict[int, AiAccountVoiceProfile] = {}
    for row in rows:
        result.setdefault(row.account_id, row)
    return result


def _latest_profile(session: Session, tenant_id: int, account_id: int) -> AiAccountVoiceProfile | None:
    return session.scalar(
        select(AiAccountVoiceProfile)
        .where(AiAccountVoiceProfile.tenant_id == tenant_id, AiAccountVoiceProfile.account_id == account_id)
        .order_by(AiAccountVoiceProfile.version.desc())
        .limit(1)
    )


def _profile_projection(account: TgAccount, profile: AiAccountVoiceProfile | None) -> dict[str, Any]:
    data = _serialize_profile(profile)
    data.update(
        {
            "account_id": account.id,
            "display_name": account.display_name,
            "username": account.username or "",
            "phone_masked": account.phone_masked,
            "account_status": account.status,
            "profile_status": profile.status if profile else "missing",
        }
    )
    return data


def _serialize_profile(profile: AiAccountVoiceProfile | None) -> dict[str, Any]:
    if not profile:
        return {"version": 0, "short_prompt_summary": ""}
    return {field: getattr(profile, field) for field in EDITABLE_PROFILE_FIELDS | {"version", "similarity_score", "updated_by", "updated_at"}}


def _require_account(session: Session, tenant_id: int, account_id: int) -> TgAccount:
    account = session.scalar(select(TgAccount).where(TgAccount.tenant_id == tenant_id, TgAccount.id == account_id, TgAccount.deleted_at.is_(None)))
    if not account:
        raise ValueError(f"account not found: {account_id}")
    return account


def _patched_profile(
    tenant_id: int,
    account_id: int,
    current: AiAccountVoiceProfile | None,
    patch: dict[str, Any],
    actor: str,
) -> AiAccountVoiceProfile:
    base = _serialize_profile(current)
    base.update({key: value for key, value in patch.items() if key in EDITABLE_PROFILE_FIELDS})
    profile = _profile_from_generated(tenant_id, account_id, base, str(base.get("short_prompt_summary") or ""))
    profile.version = int(current.version if current else 0) + 1
    profile.updated_by = actor
    return profile


def _generated_profile(
    session: Session,
    tenant_id: int,
    account_id: int,
    generator: Callable[[list[int]], list[dict[str, Any]]],
) -> AiAccountVoiceProfile:
    profiles = _profiles_by_account(generator([account_id]))
    profile = profiles.get(account_id)
    if not profile:
        raise ValueError(f"voice profile missing for account {account_id}")
    return _profile_from_generated(tenant_id, account_id, profile, _valid_summary(profile, account_id))


def _profiles_by_account(generated: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(item["account_id"]): item for item in generated if item.get("account_id") is not None}


def _batch_candidate_account_ids(session: Session, tenant_id: int, account_ids: list[int], missing_only: bool) -> list[int]:
    unique_ids = list(dict.fromkeys(int(account_id) for account_id in account_ids))
    if not unique_ids and missing_only:
        return list(
            session.scalars(
                select(TgAccount.id)
                .where(TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None), TgAccount.status == AccountStatus.ACTIVE.value)
                .order_by(TgAccount.id.asc())
            )
        )
    for account_id in unique_ids:
        _require_account(session, tenant_id, account_id)
    return unique_ids


def _audit(session: Session, tenant_id: int, actor: str, action: str, account_id: int, detail: str) -> None:
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            target_type="ai_account_voice_profile",
            target_id=str(account_id),
            detail=detail,
        )
    )


def _profile_from_generated(
    tenant_id: int,
    account_id: int,
    profile: dict[str, Any],
    summary: str,
) -> AiAccountVoiceProfile:
    return AiAccountVoiceProfile(
        tenant_id=tenant_id,
        account_id=account_id,
        version=1,
        age_band=str(profile.get("age_band") or ""),
        persona_experiences=list(profile.get("persona_experiences") or []),
        consumption_experiences=list(profile.get("consumption_experiences") or []),
        sentence_length=str(profile.get("sentence_length") or ""),
        interaction_habits=list(profile.get("interaction_habits") or []),
        tone_strength=str(profile.get("tone_strength") or ""),
        lexical_preferences=list(profile.get("lexical_preferences") or []),
        emoji_policy=str(profile.get("emoji_policy") or ""),
        forbidden_expressions=list(profile.get("forbidden_expressions") or []),
        short_prompt_summary=summary,
        source=str(profile.get("source") or "ai_batch"),
        status="active",
        quality_status="active",
        last_rebuilt_at=_now(),
    )

__all__ = [
    "VOICE_PROFILE_BATCH_SIZE", "batch_rebuild_voice_profiles", "ensure_voice_profiles_for_accounts", "generate_voice_profiles_with_ai",
    "group_stance_summaries", "list_voice_profiles", "patch_voice_profile", "rebuild_voice_profile",
    "upsert_group_stance_memory", "voice_profile_prompt_details", "voice_profile_prompt_summaries",
]
