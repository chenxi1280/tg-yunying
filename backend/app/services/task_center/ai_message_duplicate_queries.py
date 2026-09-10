"""Narrow projections for message duplicate identity and similarity inputs."""
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session
from app.models import AiGroupMessageMemory

DEDUP_STATUSES = frozenset({"pending", "reserved", "claiming", "executing", "unknown_after_send", "success"})
TEN_DAY_WINDOW = timedelta(days=10)


def _find_exact_duplicate(
    session: Session,
    *,
    tenant_id: int,
    account_id: int | None,
    fingerprint: str,
    now: datetime,
    exclude_id: str = "",
) -> Row | None:
    if account_id is None:
        return None
    cutoff = now - TEN_DAY_WINDOW
    return session.execute(
        select(AiGroupMessageMemory.id)
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
    ).first()


def _find_template_shell_duplicate(
    session: Session,
    *,
    tenant_id: int,
    account_id: int | None,
    template_shell_key: str,
    now: datetime,
    exclude_id: str = "",
) -> Row | None:
    if not template_shell_key or account_id is None:
        return None
    return session.execute(
        select(AiGroupMessageMemory.id)
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
    ).first()


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


