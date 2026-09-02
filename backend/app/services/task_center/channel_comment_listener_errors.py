from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChannelCommentListenerErrorEvent, Task, TaskSourceSubscription


def record_listener_error(
    session: Session,
    task: Task,
    subscription: TaskSourceSubscription,
    *,
    error_code: str,
    detail: str,
    observed_at: datetime,
) -> ChannelCommentListenerErrorEvent:
    event = session.scalar(select(ChannelCommentListenerErrorEvent).where(
        ChannelCommentListenerErrorEvent.task_id == task.id,
        ChannelCommentListenerErrorEvent.subscription_id == subscription.id,
        ChannelCommentListenerErrorEvent.target_reference_revision == subscription.target_reference_revision,
        ChannelCommentListenerErrorEvent.listener_revision == subscription.listener_revision,
        ChannelCommentListenerErrorEvent.error_code == error_code,
    ))
    if event is None:
        event = ChannelCommentListenerErrorEvent(
            tenant_id=task.tenant_id,
            task_id=task.id,
            subscription_id=subscription.id,
            target_reference_revision=subscription.target_reference_revision,
            listener_revision=subscription.listener_revision,
            error_code=error_code,
            error_state="active",
            detail=detail,
            observed_at=observed_at,
        )
        session.add(event)
    task.last_error = error_code
    return event


def clear_owned_listener_errors(
    session: Session,
    task: Task,
    subscription: TaskSourceSubscription,
    *,
    cleared_at: datetime,
) -> int:
    rows = session.scalars(select(ChannelCommentListenerErrorEvent).where(
        ChannelCommentListenerErrorEvent.task_id == task.id,
        ChannelCommentListenerErrorEvent.subscription_id == subscription.id,
        ChannelCommentListenerErrorEvent.target_reference_revision == subscription.target_reference_revision,
        ChannelCommentListenerErrorEvent.listener_revision == subscription.listener_revision,
        ChannelCommentListenerErrorEvent.error_state == "active",
    )).all()
    for row in rows:
        row.error_state = "cleared"
        row.cleared_at = cleared_at
    if rows and task.last_error in {row.error_code for row in rows}:
        task.last_error = ""
    return len(rows)


__all__ = ["clear_owned_listener_errors", "record_listener_error"]
