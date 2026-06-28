from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Action
from app.services._common import _now
from app.services.task_center.ai_message_memory import (
    HISTORICAL_BACKFILL_STATUSES,
    THIRTY_DAY_WINDOW,
    backfill_group_ai_message_memory_from_actions,
    expire_stale_group_ai_reservations,
)


SessionFactory = Callable[[], Session]


def drain_ai_message_memory_maintenance(
    session_factory: SessionFactory,
    limit: int = 100,
    *,
    now: datetime | None = None,
) -> int:
    current_time = now or _now()
    backfill_limit = max(1, int(limit))
    with session_factory() as session:
        expired = expire_stale_group_ai_reservations(session, now=current_time)
        created = _backfill_recent_group_ai_history(session, current_time, backfill_limit)
        session.commit()
        return expired + created


def _backfill_recent_group_ai_history(session: Session, now: datetime, limit: int) -> int:
    created = 0
    for tenant_id in _tenant_ids_with_group_ai_history(session, now, limit):
        if created >= limit:
            break
        result = backfill_group_ai_message_memory_from_actions(
            session,
            tenant_id=tenant_id,
            now=now,
            limit=max(1, limit - created),
        )
        created += int(result["created"])
    return created


def _tenant_ids_with_group_ai_history(session: Session, now: datetime, limit: int) -> list[int]:
    cutoff = now - THIRTY_DAY_WINDOW
    stmt = (
        select(Action.tenant_id)
        .where(
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status.in_(HISTORICAL_BACKFILL_STATUSES),
            or_(Action.executed_at >= cutoff, Action.scheduled_at >= cutoff, Action.created_at >= cutoff),
        )
        .distinct()
        .order_by(Action.tenant_id.asc())
        .limit(max(1, int(limit)))
    )
    return [int(tenant_id) for tenant_id in session.scalars(stmt)]


__all__ = ["drain_ai_message_memory_maintenance"]
