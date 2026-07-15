from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, TypeAlias

from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from app.models import AiGroupMessageMemory
from app.services._common import _now


MemorySimilarityRow: TypeAlias = Row | AiGroupMessageMemory
WindowLoader: TypeAlias = Callable[..., list[Row]]
INCREMENTAL_REFRESH_OVERLAP = timedelta(minutes=5)


@dataclass
class DuplicateMemoryBatch:
    now: datetime
    rows_by_tenant: dict[int, list[MemorySimilarityRow]] = field(default_factory=dict)
    refreshed_at_by_tenant: dict[int, datetime] = field(default_factory=dict)


def refresh_duplicate_memory_batch(
    session: Session,
    batch: DuplicateMemoryBatch,
    *,
    tenant_id: int,
    group_id: int,
    statuses: set[str],
    window_loader: WindowLoader,
    window: timedelta,
) -> None:
    refreshed_at = batch.refreshed_at_by_tenant.get(tenant_id)
    observed_at = _now()
    if refreshed_at is None:
        rows = window_loader(
            session, tenant_id=tenant_id, group_id=group_id, cutoff=batch.now - window,
        )
    else:
        rows = _updated_window_rows(
            session,
            tenant_id=tenant_id,
            cutoff=batch.now - window,
            updated_after=refreshed_at - INCREMENTAL_REFRESH_OVERLAP,
        )
    batch.rows_by_tenant[tenant_id] = _merge_rows(
        batch.rows_by_tenant.get(tenant_id, []), rows, statuses=statuses,
    )
    batch.refreshed_at_by_tenant[tenant_id] = observed_at


def cached_similarity_rows(
    batch: DuplicateMemoryBatch,
    *,
    tenant_id: int,
    cutoff: datetime,
) -> list[MemorySimilarityRow]:
    return [
        row for row in batch.rows_by_tenant.get(tenant_id, [])
        if _naive_datetime(row.planned_at) >= _naive_datetime(cutoff)
    ]


def _naive_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def remember_duplicate_batch_memory(
    batch: DuplicateMemoryBatch | None,
    memory: AiGroupMessageMemory,
) -> None:
    if batch is None:
        return
    rows = batch.rows_by_tenant.get(memory.tenant_id)
    if rows is not None:
        rows.insert(0, memory)


def _updated_window_rows(
    session: Session,
    *,
    tenant_id: int,
    cutoff: datetime,
    updated_after: datetime,
) -> list[Row]:
    return list(session.execute(
        select(
            AiGroupMessageMemory.id,
            AiGroupMessageMemory.normalized_text,
            AiGroupMessageMemory.raw_text,
            AiGroupMessageMemory.planned_at,
            AiGroupMessageMemory.status,
        ).where(
            AiGroupMessageMemory.tenant_id == tenant_id,
            AiGroupMessageMemory.planned_at >= cutoff,
            AiGroupMessageMemory.updated_at >= updated_after,
        ).order_by(AiGroupMessageMemory.updated_at.desc())
    ))


def _merge_rows(
    existing: list[MemorySimilarityRow],
    incoming: list[MemorySimilarityRow],
    *,
    statuses: set[str],
) -> list[MemorySimilarityRow]:
    incoming_ids = {str(row.id) for row in incoming}
    active_incoming = [row for row in incoming if row.status in statuses]
    return [*active_incoming, *(row for row in existing if str(row.id) not in incoming_ids)]


__all__ = [
    "DuplicateMemoryBatch",
    "MemorySimilarityRow",
    "cached_similarity_rows",
    "refresh_duplicate_memory_batch",
    "remember_duplicate_batch_memory",
]
