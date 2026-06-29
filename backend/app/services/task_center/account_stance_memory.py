from __future__ import annotations

import json
import logging
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AiAccountGroupStanceMemory
from app.services._common import _now
from app.services.task_center.ai_act_types import canonical_ai_group_act_type
from app.services.task_center.runtime_resources import _redis_client

logger = logging.getLogger(__name__)

STANCE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
STANCE_CACHE_PREFIX = "ai_group:stance"


def upsert_group_stance_memory(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    account_id: int,
    topic_direction: str,
    teacher_target: str,
    stance: str,
    act_type: str,
    semantic_cluster: str,
    message_id: str,
    summary: str,
) -> AiAccountGroupStanceMemory:
    now = _now()
    row = session.scalar(
        select(AiAccountGroupStanceMemory).where(
            AiAccountGroupStanceMemory.tenant_id == tenant_id,
            AiAccountGroupStanceMemory.group_id == group_id,
            AiAccountGroupStanceMemory.account_id == account_id,
        )
    )
    if not row:
        row = AiAccountGroupStanceMemory(
            tenant_id=tenant_id,
            group_id=group_id,
            account_id=account_id,
            window_start_at=now,
        )
        session.add(row)
    row.topic_direction = topic_direction
    row.teacher_target = teacher_target
    row.stance = stance
    row.last_act_type = canonical_ai_group_act_type(act_type)
    row.last_semantic_cluster = semantic_cluster
    row.last_message_id = message_id
    row.last_spoken_at = now
    row.window_end_at = now + timedelta(days=7)
    row.summary = summary
    row.updated_at = now
    session.flush()
    _refresh_stance_cache(row)
    return row


def group_stance_summaries(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    account_ids: list[int],
) -> dict[int, str]:
    unique_ids = list(dict.fromkeys(int(account_id) for account_id in account_ids))
    if not unique_ids:
        return {}
    cached, missed_ids = _cached_stance_summaries(tenant_id, group_id, unique_ids)
    if not missed_ids:
        return cached
    rows = list(session.scalars(_stance_query(tenant_id, group_id, missed_ids)))
    _refresh_stance_cache_many(rows)
    return {**cached, **{row.account_id: row.summary for row in rows if row.summary}}


def _stance_query(tenant_id: int, group_id: int, account_ids: list[int]):
    now = _now()
    return select(AiAccountGroupStanceMemory).where(
        AiAccountGroupStanceMemory.tenant_id == tenant_id,
        AiAccountGroupStanceMemory.group_id == group_id,
        AiAccountGroupStanceMemory.account_id.in_(account_ids),
        or_(AiAccountGroupStanceMemory.window_end_at.is_(None), AiAccountGroupStanceMemory.window_end_at >= now),
    )


def _cached_stance_summaries(
    tenant_id: int,
    group_id: int,
    account_ids: list[int],
) -> tuple[dict[int, str], list[int]]:
    settings = get_settings()
    if not _redis_enabled(settings):
        return {}, account_ids
    keys = [_stance_cache_key(tenant_id, group_id, account_id) for account_id in account_ids]
    try:
        values = _redis_client(settings.redis_url).mget(keys)
    except Exception as exc:  # noqa: BLE001 - DB remains the required fact source.
        logger.warning("ai_group_stance_cache_read_failed", extra={"error": str(exc)})
        return {}, account_ids
    result: dict[int, str] = {}
    missed: list[int] = []
    for account_id, value in zip(account_ids, values, strict=False):
        summary = _cached_summary(value)
        if summary:
            result[account_id] = summary
        else:
            missed.append(account_id)
    return result, missed


def _refresh_stance_cache_many(rows: list[AiAccountGroupStanceMemory]) -> None:
    for row in rows:
        _refresh_stance_cache(row)


def _refresh_stance_cache(row: AiAccountGroupStanceMemory) -> None:
    settings = get_settings()
    if not row.summary or not _redis_enabled(settings):
        return
    key = _stance_cache_key(row.tenant_id, row.group_id, row.account_id)
    try:
        _redis_client(settings.redis_url).setex(key, STANCE_CACHE_TTL_SECONDS, _cache_payload(row))
    except Exception as exc:  # noqa: BLE001 - DB remains the required fact source.
        logger.warning(
            "ai_group_stance_cache_write_failed",
            extra={"error": str(exc), "tenant_id": row.tenant_id, "group_id": row.group_id, "account_id": row.account_id},
        )


def _cached_summary(value: object) -> str:
    if not value:
        return ""
    raw = value.decode("utf-8") if isinstance(value, bytes) else str(value)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("summary") or "").strip()


def _cache_payload(row: AiAccountGroupStanceMemory) -> str:
    payload = {
        "summary": row.summary,
        "topic_direction": row.topic_direction,
        "teacher_target": row.teacher_target,
        "stance": row.stance,
        "last_act_type": canonical_ai_group_act_type(row.last_act_type),
        "last_semantic_cluster": row.last_semantic_cluster,
        "last_message_id": row.last_message_id,
        "last_spoken_at": row.last_spoken_at.isoformat() if row.last_spoken_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _redis_enabled(settings) -> bool:  # noqa: ANN001
    return bool(getattr(settings, "redis_url", "")) and str(getattr(settings, "queue_backend", "")).lower() == "redis"


def _stance_cache_key(tenant_id: int, group_id: int, account_id: int) -> str:
    return f"{STANCE_CACHE_PREFIX}:{tenant_id}:{group_id}:{account_id}"


__all__ = ["group_stance_summaries", "upsert_group_stance_memory"]
