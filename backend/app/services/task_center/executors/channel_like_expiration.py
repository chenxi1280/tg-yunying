from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import ChannelMessage, ReactionFulfillmentObligation, Task

from ..source_pacing import rolling_source_window, source_window_days


def close_expired_like_obligations(
    session: Session,
    task: Task,
    *,
    now_value: datetime,
) -> int:
    cutoff = now_value - timedelta(days=source_window_days(task))
    expired_message_ids = select(ChannelMessage.id).where(
        ChannelMessage.created_at <= cutoff,
    )
    result = session.execute(
        update(ReactionFulfillmentObligation)
        .where(
            ReactionFulfillmentObligation.task_id == task.id,
            ReactionFulfillmentObligation.status == "open",
            ReactionFulfillmentObligation.current_action_id.is_(None),
            ReactionFulfillmentObligation.channel_message_id.in_(expired_message_ids),
        )
        .values(status="closed_expired")
    )
    closed = max(0, int(result.rowcount or 0))
    if closed:
        _record_expired_settlement(task, closed)
    return closed


def active_like_messages(
    task: Task,
    messages: list[ChannelMessage],
    *,
    now_value: datetime,
) -> list[ChannelMessage]:
    return [
        message
        for message in messages
        if rolling_source_window(task, message.created_at)[1] > now_value
    ]


def _record_expired_settlement(task: Task, closed: int) -> None:
    stats = dict(task.stats or {})
    stats["window_expired_settled_count"] = int(
        stats.get("window_expired_settled_count") or 0
    ) + closed
    task.stats = stats


__all__ = ["active_like_messages", "close_expired_like_obligations"]
