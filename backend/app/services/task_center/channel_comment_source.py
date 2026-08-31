from __future__ import annotations

from datetime import datetime

from app.models import ChannelMessage, Task

from .fulfillment_activation import CURRENT_CONTRACT_VERSION
from .source_pacing import rolling_source_window


SOURCE_PUBLISHED_AT_UNPROVEN = "source_published_at_unproven"


def comment_source_published_at(task: Task, message: ChannelMessage) -> datetime | None:
    if message.published_at is not None:
        return message.published_at
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        task.last_error = SOURCE_PUBLISHED_AT_UNPROVEN
        return None
    return message.created_at


def comment_source_window(
    task: Task,
    message: ChannelMessage,
) -> tuple[datetime, datetime] | None:
    published_at = comment_source_published_at(task, message)
    if published_at is None:
        return None
    return rolling_source_window(task, published_at)


__all__ = [
    "SOURCE_PUBLISHED_AT_UNPROVEN",
    "comment_source_published_at",
    "comment_source_window",
]
