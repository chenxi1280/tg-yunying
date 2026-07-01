from __future__ import annotations

import json
import logging
from typing import Any

from app.config import get_settings
from app.models import AiAccountVoiceProfile
from app.services.task_center.runtime_resources import _redis_client

logger = logging.getLogger(__name__)

VOICE_PROFILE_CACHE_PREFIX = "ai_group:voice_profile"
VOICE_PROFILE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60


def cached_voice_profile_prompt_details(
    tenant_id: int,
    account_ids: list[int],
) -> tuple[dict[int, dict[str, Any]], list[int]]:
    unique_ids = _unique_account_ids(account_ids)
    settings = get_settings()
    if not _redis_enabled(settings):
        return {}, unique_ids
    keys = [_voice_profile_cache_key(tenant_id, account_id) for account_id in unique_ids]
    try:
        values = _redis_client(settings.redis_url).mget(keys)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai_group_voice_profile_cache_read_failed", extra={"error": str(exc)})
        return {}, unique_ids
    cached: dict[int, dict[str, Any]] = {}
    missed: list[int] = []
    for account_id, value in zip(unique_ids, values, strict=False):
        detail = _parse_cached_detail(value)
        if detail is None:
            missed.append(account_id)
            continue
        cached[account_id] = detail
    return cached, missed


def refresh_voice_profile_cache_many(rows: list[AiAccountVoiceProfile]) -> None:
    for row in rows:
        refresh_voice_profile_cache(row)


def refresh_voice_profile_cache(row: AiAccountVoiceProfile) -> None:
    if not _cacheable(row):
        delete_voice_profile_cache(row.tenant_id, row.account_id)
        return
    settings = get_settings()
    if not _redis_enabled(settings):
        return
    try:
        _redis_client(settings.redis_url).setex(
            _voice_profile_cache_key(row.tenant_id, row.account_id),
            VOICE_PROFILE_CACHE_TTL_SECONDS,
            _cache_payload(row),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ai_group_voice_profile_cache_write_failed",
            extra={"tenant_id": row.tenant_id, "account_id": row.account_id, "error": str(exc)},
        )


def delete_voice_profile_cache(tenant_id: int, account_id: int) -> None:
    settings = get_settings()
    if not _redis_enabled(settings):
        return
    try:
        _redis_client(settings.redis_url).delete(_voice_profile_cache_key(tenant_id, account_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ai_group_voice_profile_cache_delete_failed",
            extra={"tenant_id": tenant_id, "account_id": account_id, "error": str(exc)},
        )


def _cacheable(row: AiAccountVoiceProfile) -> bool:
    return row.status == "active" and row.quality_status == "active" and bool(row.short_prompt_summary)


def _cache_payload(row: AiAccountVoiceProfile) -> str:
    return json.dumps(
        {
            "account_id": row.account_id,
            "version": int(row.version or 0),
            "summary": row.short_prompt_summary,
            "mask_name": row.mask_name,
            "audience_archetype": row.audience_archetype,
            "identity_frame": row.identity_frame,
            "preference_tags": row.preference_tags or [],
        },
        ensure_ascii=False,
    )


def _parse_cached_detail(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    raw = value.decode("utf-8") if isinstance(value, bytes) else str(value)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    version = payload.get("version")
    summary = str(payload.get("summary") or "")
    if not summary or not _has_mask_payload(payload):
        return None
    return {
        "version": int(version or 0),
        "summary": summary,
        "mask_name": str(payload.get("mask_name") or ""),
        "audience_archetype": str(payload.get("audience_archetype") or ""),
        "identity_frame": str(payload.get("identity_frame") or ""),
        "preference_tags": _string_list(payload.get("preference_tags")),
    }


def _has_mask_payload(payload: dict[str, Any]) -> bool:
    return all(key in payload for key in ("mask_name", "audience_archetype", "identity_frame", "preference_tags"))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _redis_enabled(settings) -> bool:  # noqa: ANN001
    return bool(getattr(settings, "redis_url", "")) and str(getattr(settings, "queue_backend", "")).lower() == "redis"


def _unique_account_ids(account_ids: list[int]) -> list[int]:
    return list(dict.fromkeys(int(account_id) for account_id in account_ids))


def _voice_profile_cache_key(tenant_id: int, account_id: int) -> str:
    return f"{VOICE_PROFILE_CACHE_PREFIX}:{tenant_id}:{account_id}"


__all__ = [
    "cached_voice_profile_prompt_details",
    "delete_voice_profile_cache",
    "refresh_voice_profile_cache",
    "refresh_voice_profile_cache_many",
]
