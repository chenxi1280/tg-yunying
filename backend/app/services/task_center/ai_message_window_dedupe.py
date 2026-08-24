from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AiGroupMessageMemory


GROUP_EXACT_WINDOW = timedelta(minutes=5)


def find_group_window_exact_duplicate(
    session: Session,
    *,
    tenant_id: int,
    group_id: int,
    fingerprint: str,
    now: datetime,
    statuses: set[str],
    exclude_id: str = "",
) -> AiGroupMessageMemory | None:
    return session.scalar(
        select(AiGroupMessageMemory)
        .where(
            AiGroupMessageMemory.tenant_id == tenant_id,
            AiGroupMessageMemory.group_id == group_id,
            AiGroupMessageMemory.text_fingerprint == fingerprint,
            AiGroupMessageMemory.status.in_(statuses),
            AiGroupMessageMemory.planned_at >= now - GROUP_EXACT_WINDOW,
            AiGroupMessageMemory.id != exclude_id,
        )
        .order_by(AiGroupMessageMemory.planned_at.desc())
        .limit(1)
    )


def group_window_reservation_key(
    tenant_id: int,
    group_id: int,
    fingerprint: str,
    now: datetime,
) -> str:
    bucket = int(now.timestamp()) // int(GROUP_EXACT_WINDOW.total_seconds())
    return f"{tenant_id}:{group_id}:{fingerprint}:{bucket}"


__all__ = [
    "find_group_window_exact_duplicate",
    "group_window_reservation_key",
]
