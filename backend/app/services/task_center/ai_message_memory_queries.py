from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Action, AiGroupMessageMemory


HISTORICAL_BACKFILL_STATUSES = {"success", "unknown_after_send"}
THIRTY_DAY_WINDOW = timedelta(days=30)


def _historical_group_ai_actions(
    session: Session,
    *,
    tenant_id: int,
    now: datetime,
    limit: int,
) -> list[Action]:
    cutoff = now - THIRTY_DAY_WINDOW
    memory_exists = select(AiGroupMessageMemory.id).where(
        AiGroupMessageMemory.action_id == Action.id,
    ).exists()
    candidate_actions = (
        select(Action.id.label("action_id"), Action.created_at.label("candidate_created_at"))
        .where(
            Action.tenant_id == tenant_id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status.in_(HISTORICAL_BACKFILL_STATUSES),
            or_(Action.executed_at >= cutoff, Action.scheduled_at >= cutoff, Action.created_at >= cutoff),
            ~memory_exists,
        )
        .order_by(Action.created_at.asc())
        .limit(max(1, int(limit)))
        .subquery()
    )
    statement = (
        select(Action)
        .join(candidate_actions, candidate_actions.c.action_id == Action.id)
        .order_by(candidate_actions.c.candidate_created_at.asc())
    )
    return list(session.scalars(statement))


def _memory_exists_for_action(session: Session, action_id: str) -> bool:
    statement = select(AiGroupMessageMemory.id).where(
        AiGroupMessageMemory.action_id == action_id,
    ).limit(1)
    return bool(session.scalar(statement))
