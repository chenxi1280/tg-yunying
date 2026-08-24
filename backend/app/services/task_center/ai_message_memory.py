from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Action, AiGroupMessageMemory
from app.services._common import _now
from app.services.task_center.ai_message_memory_queries import (
    HISTORICAL_BACKFILL_STATUSES as HISTORICAL_BACKFILL_STATUSES,
    THIRTY_DAY_WINDOW,
    _historical_group_ai_actions,
    _memory_exists_for_action,
)
from app.services.task_center.ai_message_memory_batch import (
    DuplicateMemoryBatch,
    MemorySimilarityRow,
    cached_similarity_rows,
    refresh_duplicate_memory_batch,
    remember_duplicate_batch_memory,
)
from app.services.task_center.ai_message_window_dedupe import (
    find_group_window_exact_duplicate,
    group_window_reservation_key,
)
from app.services.task_center.ai_message_memory_text import (
    message_identity,
    normalize_group_ai_text,
    semantic_cluster,
    template_shell_key,
    text_fingerprint,
    text_similarity_reaches,
)

DEDUP_STATUSES = {"pending", "reserved", "claiming", "executing", "unknown_after_send", "success"}
DEFAULT_RESERVATION_TTL = timedelta(minutes=30)
TEN_DAY_WINDOW = timedelta(days=10)
HIGH_SIMILARITY_THRESHOLD = 0.78
SEMANTIC_SIMILARITY_THRESHOLD = 0.80
@dataclass
class DuplicateMessageReservation(Exception):
    reference_id: str
    duplicate_window: str


def reserve_group_ai_message(
    session: Session, *, tenant_id: int, group_id: int, task_id: str, account_id: int | None,
    raw_text: str, now: datetime | None = None, reservation_ttl: timedelta = DEFAULT_RESERVATION_TTL,
    topic_direction: str = "", teacher_target: str = "", profile_version: int | None = None,
    profile_match_score: int | None = None, profile_match_reason: str = "",
    duplicate_batch: DuplicateMemoryBatch | None = None,
    account_mask_id: str = "", account_mask_version: int | None = None,
    mask_contract_version: str = "", mask_snapshot_hash: str = "",
    mask_status: str = "active",
    content_source: str = "account_mask",
) -> AiGroupMessageMemory:
    current_time = now or (duplicate_batch.now if duplicate_batch else _now())
    normalized, fingerprint, semantic_cluster_value, template_shell = message_identity(raw_text)
    duplicate, duplicate_window = _find_duplicate(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        group_id=group_id,
        fingerprint=fingerprint,
        normalized=normalized,
        template_shell_key=template_shell,
        now=current_time,
        duplicate_batch=duplicate_batch,
    )
    if duplicate:
        raise DuplicateMessageReservation(reference_id=duplicate.id, duplicate_window=duplicate_window)
    memory = _new_reserved_memory(
        tenant_id=tenant_id,
        account_id=account_id,
        group_id=group_id,
        task_id=task_id,
        raw_text=raw_text,
        normalized=normalized,
        fingerprint=fingerprint,
        semantic_cluster=semantic_cluster_value,
        template_shell_key=template_shell,
        current_time=current_time,
        reservation_ttl=reservation_ttl,
        topic_direction=topic_direction,
        teacher_target=teacher_target,
        profile_version=profile_version,
        profile_match_score=profile_match_score,
        profile_match_reason=profile_match_reason,
        account_mask_id=account_mask_id,
        account_mask_version=account_mask_version,
        mask_contract_version=mask_contract_version,
        mask_snapshot_hash=mask_snapshot_hash,
        mask_status=mask_status,
        content_source=content_source,
    )
    _persist_reserved_memory(
        session,
        memory,
        tenant_id=tenant_id,
        group_id=group_id,
        fingerprint=fingerprint,
        current_time=current_time,
        duplicate_batch=duplicate_batch,
    )
    return memory


def _persist_reserved_memory(
    session: Session,
    memory: AiGroupMessageMemory,
    *,
    tenant_id: int,
    group_id: int,
    fingerprint: str,
    current_time: datetime,
    duplicate_batch: DuplicateMemoryBatch | None,
) -> None:
    try:
        with session.begin_nested():
            session.add(memory)
            session.flush()
    except IntegrityError as exc:
        duplicate = _find_exact_duplicate(
            session,
            tenant_id,
            memory.account_id,
            fingerprint,
            current_time,
        )
        if duplicate:
            raise DuplicateMessageReservation(
                reference_id=duplicate.id, duplicate_window="5m_exact",
            ) from exc
        duplicate = find_group_window_exact_duplicate(
            session,
            tenant_id=tenant_id,
            group_id=group_id,
            fingerprint=fingerprint,
            now=current_time,
            statuses=DEDUP_STATUSES,
        )
        if duplicate:
            raise DuplicateMessageReservation(
                reference_id=duplicate.id, duplicate_window="5m_group_exact",
            ) from exc
        raise
    remember_duplicate_batch_memory(duplicate_batch, memory)


def _new_reserved_memory(
    *,
    tenant_id: int,
    group_id: int,
    task_id: str,
    account_id: int | None,
    raw_text: str,
    normalized: str,
    fingerprint: str,
    semantic_cluster: str,
    template_shell_key: str,
    current_time: datetime,
    reservation_ttl: timedelta,
    topic_direction: str,
    teacher_target: str,
    profile_version: int | None,
    profile_match_score: int | None,
    profile_match_reason: str,
    account_mask_id: str,
    account_mask_version: int | None,
    mask_contract_version: str,
    mask_snapshot_hash: str,
    mask_status: str,
    content_source: str,
) -> AiGroupMessageMemory:
    return AiGroupMessageMemory(
        tenant_id=tenant_id,
        group_id=group_id,
        task_id=task_id,
        account_id=account_id,
        topic_direction=topic_direction,
        teacher_target=teacher_target,
        raw_text=raw_text,
        normalized_text=normalized,
        text_fingerprint=fingerprint,
        semantic_cluster=semantic_cluster,
        template_shell_key=template_shell_key,
        reservation_key=group_window_reservation_key(
            tenant_id, group_id, fingerprint, current_time,
        ),
        status="reserved",
        planned_at=current_time,
        expires_at=current_time + reservation_ttl,
        duplicate_window="5m_exact",
        quality_decision="reserved",
        profile_version=profile_version,
        profile_match_score=profile_match_score,
        profile_match_reason=profile_match_reason,
        account_mask_id=account_mask_id,
        account_mask_version=account_mask_version,
        mask_contract_version=mask_contract_version,
        mask_snapshot_hash=mask_snapshot_hash,
        mask_status=mask_status,
        content_source=content_source,
    )


def mark_group_ai_message_result(
    session: Session,
    memory_id: str,
    *,
    status: str,
    action_id: str | None = None,
    sent_at: datetime | None = None,
    result: dict | None = None,
) -> AiGroupMessageMemory:
    memory = session.get(AiGroupMessageMemory, memory_id)
    if not memory:
        raise ValueError(f"ai group message memory not found: {memory_id}")
    memory.status = status
    if action_id is not None:
        memory.action_id = action_id
    if sent_at is not None:
        memory.sent_at = sent_at
    if result is not None:
        memory.result = result
    memory.updated_at = _now()
    return memory


def ensure_group_ai_message_sendable(
    session: Session,
    memory_id: str,
    *,
    now: datetime | None = None,
) -> AiGroupMessageMemory:
    memory = session.get(AiGroupMessageMemory, memory_id)
    if not memory:
        raise ValueError(f"ai group message memory not found: {memory_id}")
    current_time = now or _now()
    duplicate, duplicate_window = _find_duplicate(
        session,
        tenant_id=memory.tenant_id,
        account_id=memory.account_id,
        group_id=memory.group_id,
        fingerprint=memory.text_fingerprint,
        normalized=memory.normalized_text or normalize_group_ai_text(memory.raw_text),
        template_shell_key=memory.template_shell_key,
        now=current_time,
        exclude_id=memory.id,
    )
    if duplicate:
        raise DuplicateMessageReservation(reference_id=duplicate.id, duplicate_window=duplicate_window)
    return memory


def expire_stale_group_ai_reservations(session: Session, *, now: datetime | None = None) -> int:
    current_time = now or _now()
    rows = list(
        session.scalars(
            select(AiGroupMessageMemory).where(
                AiGroupMessageMemory.status == "reserved",
                AiGroupMessageMemory.expires_at.is_not(None),
                AiGroupMessageMemory.expires_at <= current_time,
            )
        )
    )
    for memory in rows:
        memory.status = "expired_before_send"
        memory.quality_decision = "expired_visible"
        memory.updated_at = current_time
    return len(rows)


def backfill_group_ai_message_memory_from_actions(
    session: Session,
    *,
    tenant_id: int,
    now: datetime | None = None,
    limit: int = 1000,
) -> dict[str, int]:
    current_time = now or _now()
    counters = {"created": 0, "skipped_existing": 0, "skipped_invalid": 0}
    for action in _historical_group_ai_actions(session, tenant_id=tenant_id, now=current_time, limit=limit):
        if _memory_exists_for_action(session, action.id):
            counters["skipped_existing"] += 1
            continue
        memory = _memory_from_historical_action(action)
        if memory is None:
            counters["skipped_invalid"] += 1
            continue
        session.add(memory)
        counters["created"] += 1
    session.flush()
    return counters


def _memory_from_historical_action(action: Action) -> AiGroupMessageMemory | None:
    payload = action.payload or {}
    raw_text = str(payload.get("message_text") or payload.get("original_text") or "").strip()
    group_id = _as_int(payload.get("group_id"))
    if not raw_text or not group_id:
        return None
    planned_at = action.scheduled_at or action.executed_at or action.created_at or _now()
    normalized = normalize_group_ai_text(raw_text)
    return AiGroupMessageMemory(
        tenant_id=action.tenant_id,
        group_id=group_id,
        task_id=action.task_id,
        action_id=action.id,
        account_id=action.account_id,
        topic_direction=_payload_label(payload.get("topic_direction"), "title"),
        teacher_target=_payload_label(payload.get("teacher_target"), "name"),
        raw_text=raw_text,
        normalized_text=normalized,
        text_fingerprint=text_fingerprint(normalized),
        semantic_cluster=str(payload.get("semantic_cluster") or semantic_cluster(normalized)),
        template_shell_key=template_shell_key(normalized),
        reservation_key="",
        status=action.status,
        planned_at=planned_at,
        sent_at=action.executed_at or planned_at,
        expires_at=planned_at + THIRTY_DAY_WINDOW,
        quality_decision="historical_backfill",
        profile_version=_as_optional_int(payload.get("profile_version") or payload.get("account_voice_profile_version")),
        profile_match_score=_as_optional_int(payload.get("profile_match_score")),
        profile_match_reason=str(payload.get("profile_match_reason") or ""),
        result=dict(action.result or {}),
    )


def _payload_label(value: object, key: str) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get(key) or "").strip()


def _as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_optional_int(value: object) -> int | None:
    number = _as_int(value)
    return number if number else None


def _find_exact_duplicate(
    session: Session,
    tenant_id: int,
    account_id: int | None,
    fingerprint: str,
    now: datetime,
    exclude_id: str = "",
) -> AiGroupMessageMemory | None:
    if account_id is None:
        return None
    cutoff = now - TEN_DAY_WINDOW
    return session.scalar(
        select(AiGroupMessageMemory)
        .where(
            AiGroupMessageMemory.tenant_id == tenant_id,
            AiGroupMessageMemory.account_id == account_id,
            AiGroupMessageMemory.text_fingerprint == fingerprint,
            AiGroupMessageMemory.status.in_(DEDUP_STATUSES),
            AiGroupMessageMemory.planned_at >= cutoff,
            AiGroupMessageMemory.id != exclude_id,
        )
        .order_by(AiGroupMessageMemory.planned_at.desc())
        .limit(1)
    )


def _find_duplicate(
    session: Session,
    *,
    tenant_id: int,
    account_id: int | None,
    group_id: int,
    fingerprint: str,
    normalized: str,
    template_shell_key: str,
    now: datetime,
    exclude_id: str = "",
    duplicate_batch: DuplicateMemoryBatch | None = None,
) -> tuple[AiGroupMessageMemory | Row | None, str]:
    exact = _find_exact_duplicate(session, tenant_id, account_id, fingerprint, now, exclude_id)
    if exact:
        return exact, "10d_exact"
    group_exact = find_group_window_exact_duplicate(
        session,
        tenant_id=tenant_id,
        group_id=group_id,
        fingerprint=fingerprint,
        now=now,
        statuses=DEDUP_STATUSES,
        exclude_id=exclude_id,
    )
    if group_exact:
        return group_exact, "5m_group_exact"
    if duplicate_batch is not None and not exclude_id and account_id is not None:
        refresh_duplicate_memory_batch(
            session,
            duplicate_batch,
            tenant_id=tenant_id,
            account_id=account_id,
            statuses=DEDUP_STATUSES,
            window=TEN_DAY_WINDOW,
            window_loader=_window_memories,
        )
    similar = _find_similar_duplicate(
        session, tenant_id, account_id, normalized, now, exclude_id, duplicate_batch,
    )
    if similar:
        return similar, "10d_similar"
    semantic = _find_semantic_duplicate(
        session, tenant_id, account_id, normalized, now, exclude_id, duplicate_batch,
    )
    if semantic:
        return semantic, "10d_semantic"
    template = _find_template_shell_duplicate(
        session, tenant_id, account_id, template_shell_key, now, exclude_id,
    )
    if template:
        return template, "10d_template_shell"
    return None, ""


def _find_similar_duplicate(
    session: Session,
    tenant_id: int,
    account_id: int | None,
    normalized: str,
    now: datetime,
    exclude_id: str = "",
    duplicate_batch: DuplicateMemoryBatch | None = None,
) -> MemorySimilarityRow | None:
    return _first_similar_memory(
        _similarity_window_memories(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            cutoff=now - TEN_DAY_WINDOW,
            exclude_id=exclude_id,
            duplicate_batch=duplicate_batch,
        ),
        normalized,
        HIGH_SIMILARITY_THRESHOLD,
    )


def _find_semantic_duplicate(
    session: Session,
    tenant_id: int,
    account_id: int | None,
    normalized: str,
    now: datetime,
    exclude_id: str = "",
    duplicate_batch: DuplicateMemoryBatch | None = None,
) -> MemorySimilarityRow | None:
    return _first_similar_memory(
        _similarity_window_memories(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            cutoff=now - TEN_DAY_WINDOW,
            exclude_id=exclude_id,
            duplicate_batch=duplicate_batch,
        ),
        normalized,
        SEMANTIC_SIMILARITY_THRESHOLD,
    )


def _find_template_shell_duplicate(
    session: Session,
    tenant_id: int,
    account_id: int | None,
    template_shell_key: str,
    now: datetime,
    exclude_id: str = "",
) -> AiGroupMessageMemory | None:
    if not template_shell_key or account_id is None:
        return None
    return session.scalar(
        select(AiGroupMessageMemory)
        .where(
            AiGroupMessageMemory.tenant_id == tenant_id,
            AiGroupMessageMemory.account_id == account_id,
            AiGroupMessageMemory.template_shell_key == template_shell_key,
            AiGroupMessageMemory.status.in_(DEDUP_STATUSES),
            AiGroupMessageMemory.planned_at >= now - TEN_DAY_WINDOW,
            AiGroupMessageMemory.id != exclude_id,
        )
        .order_by(AiGroupMessageMemory.planned_at.desc())
        .limit(1)
    )


def _window_memories(
    session: Session,
    *,
    tenant_id: int,
    account_id: int | None,
    cutoff: datetime,
    exclude_id: str = "",
) -> list[Row]:
    if account_id is None:
        return []
    return list(
        session.execute(
            select(
                AiGroupMessageMemory.id,
                AiGroupMessageMemory.normalized_text,
                AiGroupMessageMemory.raw_text,
                AiGroupMessageMemory.planned_at,
                AiGroupMessageMemory.status,
            )
            .where(
                AiGroupMessageMemory.tenant_id == tenant_id,
                AiGroupMessageMemory.account_id == account_id,
                AiGroupMessageMemory.status.in_(DEDUP_STATUSES),
                AiGroupMessageMemory.planned_at >= cutoff,
                AiGroupMessageMemory.id != exclude_id,
            )
            .order_by(AiGroupMessageMemory.planned_at.desc())
        )
    )


def _similarity_window_memories(
    session: Session,
    *,
    tenant_id: int,
    account_id: int | None,
    cutoff: datetime,
    exclude_id: str,
    duplicate_batch: DuplicateMemoryBatch | None,
) -> list[MemorySimilarityRow]:
    if duplicate_batch is not None and not exclude_id and account_id is not None:
        return cached_similarity_rows(
            duplicate_batch,
            tenant_id=tenant_id,
            account_id=account_id,
            cutoff=cutoff,
        )
    return _window_memories(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        cutoff=cutoff,
        exclude_id=exclude_id,
    )


def _first_similar_memory(
    rows: list[MemorySimilarityRow],
    normalized: str,
    threshold: float,
) -> MemorySimilarityRow | None:
    for row in rows:
        if text_similarity_reaches(
            normalized,
            row.normalized_text or normalize_group_ai_text(row.raw_text),
            threshold,
        ):
            return row
    return None


__all__ = [
    "DuplicateMessageReservation",
    "backfill_group_ai_message_memory_from_actions",
    "ensure_group_ai_message_sendable",
    "expire_stale_group_ai_reservations",
    "mark_group_ai_message_result",
    "normalize_group_ai_text",
    "reserve_group_ai_message",
]
