"""Read only the immutable values needed for managed presence statistics."""
from dataclasses import dataclass
from datetime import datetime
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.models import Action, ContextTurn, ConversationEvent, Task, TaskDayLedger, TgGroup, UnownedOutboundActivityObservation


@dataclass(frozen=True, kw_only=True)
class PresenceAction:
    status: str
    visibility_status: object
    executed_at: datetime | None
    scheduled_at: datetime


def _external_turns(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    group: TgGroup,
) -> list[datetime]:
    return list(session.scalars(
        select(ContextTurn.last_event_at)
        .join(ConversationEvent, ConversationEvent.id == ContextTurn.anchor_event_id)
        .where(
            ContextTurn.tenant_id == task.tenant_id,
            ContextTurn.surface == "group_ai_chat",
            ContextTurn.canonical_peer_id == str(group.tg_peer_id),
            ContextTurn.state == "closed",
            ConversationEvent.author_class == "external_human",
            ConversationEvent.is_current.is_(True),
            ConversationEvent.deleted_at.is_(None),
            ContextTurn.last_event_at >= ledger.period_start_at,
            ContextTurn.last_event_at < ledger.deadline_at,
        )
    ))


def _managed_actions(session: Session, task: Task, ledger: TaskDayLedger, *, group: TgGroup) -> list[PresenceAction]:
    statement = select(
        Action.payload["group_id"].label("group_value"), Action.status,
        Action.result["visibility_status"].label("visibility_status"),
        Action.executed_at, Action.scheduled_at,
    ).where(
        Action.tenant_id == task.tenant_id,
        Action.task_type == "group_ai_chat", Action.action_type == "send_message",
        func.coalesce(Action.executed_at, Action.scheduled_at) >= ledger.period_start_at,
        func.coalesce(Action.executed_at, Action.scheduled_at) < ledger.deadline_at,
    )
    return [PresenceAction(status=row.status, visibility_status=row.visibility_status,
                           executed_at=row.executed_at, scheduled_at=row.scheduled_at)
            for row in session.execute(statement)
            if int(row.group_value or 0) == int(group.id)]


def _unowned_authored(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    group: TgGroup,
) -> list[datetime]:
    return list(session.scalars(
        select(UnownedOutboundActivityObservation.observed_at).where(
            UnownedOutboundActivityObservation.tenant_id == task.tenant_id,
            UnownedOutboundActivityObservation.activity_class == "authored_message",
            UnownedOutboundActivityObservation.canonical_peer_id == str(group.tg_peer_id),
            UnownedOutboundActivityObservation.observed_at >= ledger.period_start_at,
            UnownedOutboundActivityObservation.observed_at < ledger.deadline_at,
        )
    ))


