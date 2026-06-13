from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, MessageTask


TIMELINE_BUCKET_SECONDS = 3600


def next_occupied_at_from_timeline(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    scheduled_at: datetime,
    cooldown_seconds: int,
    exclude_action_ids: set[str],
    exclude_message_task_id: int | None,
    cache: Any,
    action_statuses: set[str],
    message_statuses: set[str],
) -> datetime | None:
    bucket_start = _bucket_start(scheduled_at)
    bucket_end = bucket_start + timedelta(seconds=TIMELINE_BUCKET_SECONDS + cooldown_seconds)
    cache_key = (
        tenant_id,
        account_id,
        bucket_start,
        bucket_end,
        tuple(sorted(exclude_action_ids)),
        exclude_message_task_id or 0,
    )
    if cache_key not in cache.occupied_timelines:
        cache.occupied_timelines[cache_key] = _load_occupied_timeline(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            start_at=bucket_start,
            end_at=bucket_end,
            exclude_action_ids=exclude_action_ids,
            exclude_message_task_id=exclude_message_task_id,
            action_statuses=action_statuses,
            message_statuses=message_statuses,
        )
    window_end = scheduled_at + timedelta(seconds=cooldown_seconds)
    return next((value for value in cache.occupied_timelines[cache_key] if scheduled_at < value < window_end), None)


def _load_occupied_timeline(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    start_at: datetime,
    end_at: datetime,
    exclude_action_ids: set[str],
    exclude_message_task_id: int | None,
    action_statuses: set[str],
    message_statuses: set[str],
) -> tuple[datetime, ...]:
    action_occupied_at = func.coalesce(Action.executed_at, Action.scheduled_at)
    action_filters = [
        Action.tenant_id == tenant_id,
        Action.account_id == account_id,
        Action.status.in_(action_statuses),
        action_occupied_at >= start_at,
        action_occupied_at < end_at,
    ]
    if exclude_action_ids:
        action_filters.append(Action.id.not_in(exclude_action_ids))
    action_times = session.scalars(select(action_occupied_at).where(*action_filters))
    message_account_id = func.coalesce(MessageTask.account_id, MessageTask.preferred_account_id)
    message_occupied_at = func.coalesce(MessageTask.sent_at, MessageTask.scheduled_at)
    message_filters = [
        MessageTask.tenant_id == tenant_id,
        message_account_id == account_id,
        MessageTask.status.in_(message_statuses),
        message_occupied_at >= start_at,
        message_occupied_at < end_at,
    ]
    if exclude_message_task_id:
        message_filters.append(MessageTask.id != exclude_message_task_id)
    message_times = session.scalars(select(message_occupied_at).where(*message_filters))
    return tuple(sorted(_naive(value) for value in [*action_times, *message_times] if value is not None))


def _bucket_start(value: datetime) -> datetime:
    return _naive(value).replace(minute=0, second=0, microsecond=0)


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value
