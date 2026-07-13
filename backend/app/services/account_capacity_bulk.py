from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, MessageTask, SchedulingSetting
from app.services.ai_config import get_scheduling_setting

TIMELINE_BUCKET_SECONDS = 3600


def capacity_setting(session: Session, tenant_id: int, cache: Any | None) -> SchedulingSetting:
    if cache and tenant_id in cache.settings:
        return cache.settings[tenant_id]
    setting = get_scheduling_setting(session, tenant_id)
    if cache:
        cache.settings[tenant_id] = setting
    return setting


def prime_capacity_cache(
    session: Session,
    *,
    tenant_id: int,
    account_ids: list[int],
    scheduled_at: datetime,
    setting: SchedulingSetting,
    exclude_action_ids: set[str],
    exclude_message_task_id: int | None,
    action_statuses: set[str],
    message_statuses: set[str],
    cache: Any,
) -> None:
    marker = _prime_marker(
        tenant_id=tenant_id,
        account_ids=account_ids,
        scheduled_at=scheduled_at,
        exclude_ids=exclude_action_ids,
        exclude_message_id=exclude_message_task_id,
    )
    if marker in cache.primed_windows:
        return
    cache.primed_windows.add(marker)
    if not _capacity_limits_enabled(setting):
        return
    windows = _capacity_windows(scheduled_at, int(setting.default_account_cooldown_seconds or 0))
    events = _load_occupancy_events(
        session,
        tenant_id=tenant_id,
        account_ids=account_ids,
        start_at=windows["load"][0],
        end_at=windows["load"][1],
        exclude_action_ids=exclude_action_ids,
        exclude_message_task_id=exclude_message_task_id,
        action_statuses=action_statuses,
        message_statuses=message_statuses,
    )
    _populate_cache(
        cache=cache,
        tenant_id=tenant_id,
        account_ids=account_ids,
        windows=windows,
        events=events,
        exclude_ids=exclude_action_ids,
        exclude_message_id=exclude_message_task_id,
    )


def _load_occupancy_events(
    session: Session,
    *,
    tenant_id: int,
    account_ids: list[int],
    start_at: datetime,
    end_at: datetime,
    exclude_action_ids: set[str],
    exclude_message_task_id: int | None,
    action_statuses: set[str],
    message_statuses: set[str],
) -> dict[int, tuple[datetime, ...]]:
    events: dict[int, list[datetime]] = {account_id: [] for account_id in account_ids}
    for account_id, occupied_at in session.execute(
        _action_occupancy_query(
            tenant_id=tenant_id,
            account_ids=account_ids,
            start_at=start_at,
            end_at=end_at,
            exclude_ids=exclude_action_ids,
            statuses=action_statuses,
        )
    ):
        events[int(account_id)].append(_naive(occupied_at))
    for account_id, occupied_at in session.execute(
        _message_occupancy_query(
            tenant_id=tenant_id,
            account_ids=account_ids,
            start_at=start_at,
            end_at=end_at,
            exclude_id=exclude_message_task_id,
            statuses=message_statuses,
        )
    ):
        events[int(account_id)].append(_naive(occupied_at))
    return {account_id: tuple(sorted(values)) for account_id, values in events.items()}


def _action_occupancy_query(*, tenant_id, account_ids, start_at, end_at, exclude_ids, statuses):
    occupied_at = func.coalesce(Action.executed_at, Action.scheduled_at)
    filters = [
        Action.tenant_id == tenant_id,
        Action.account_id.in_(account_ids),
        Action.status.in_(statuses),
        occupied_at >= start_at,
        occupied_at < end_at,
    ]
    if exclude_ids:
        filters.append(Action.id.not_in(exclude_ids))
    return select(Action.account_id, occupied_at).where(*filters)


def _message_occupancy_query(*, tenant_id, account_ids, start_at, end_at, exclude_id, statuses):
    account_id = func.coalesce(MessageTask.account_id, MessageTask.preferred_account_id)
    occupied_at = func.coalesce(MessageTask.sent_at, MessageTask.scheduled_at)
    filters = [
        MessageTask.tenant_id == tenant_id,
        account_id.in_(account_ids),
        MessageTask.status.in_(statuses),
        occupied_at >= start_at,
        occupied_at < end_at,
    ]
    if exclude_id:
        filters.append(MessageTask.id != exclude_id)
    return select(account_id, occupied_at).where(*filters)


def _populate_cache(*, cache, tenant_id, account_ids, windows, events, exclude_ids, exclude_message_id) -> None:
    exclusion_key = (tuple(sorted(exclude_ids)), exclude_message_id or 0)
    for account_id in account_ids:
        values = events.get(account_id, ())
        for window_name in ("hour", "day"):
            start_at, end_at = windows[window_name]
            key = (tenant_id, account_id, start_at, end_at, *exclusion_key)
            cache.occupied_counts[key] = sum(start_at <= value < end_at for value in values)
        for window_name in ("past_timeline", "future_timeline"):
            start_at, end_at = windows[window_name]
            key = (tenant_id, account_id, start_at, end_at, *exclusion_key)
            cache.occupied_timelines[key] = tuple(value for value in values if start_at <= value < end_at)


def _capacity_windows(scheduled_at: datetime, cooldown_seconds: int) -> dict[str, tuple[datetime, datetime]]:
    at = _naive(scheduled_at)
    hour_start = at.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + timedelta(hours=1)
    day_start = at.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    past_start = hour_start - timedelta(seconds=cooldown_seconds)
    future_end = hour_end + timedelta(seconds=cooldown_seconds)
    return {
        "hour": (hour_start, hour_end),
        "day": (day_start, day_end),
        "past_timeline": (past_start, hour_end),
        "future_timeline": (hour_start, future_end),
        "load": (min(day_start, past_start), max(day_end, future_end)),
    }


def _prime_marker(*, tenant_id, account_ids, scheduled_at, exclude_ids, exclude_message_id) -> tuple:
    return (
        tenant_id,
        tuple(sorted(account_ids)),
        _naive(scheduled_at).replace(minute=0, second=0, microsecond=0),
        tuple(sorted(exclude_ids)),
        exclude_message_id or 0,
    )


def _capacity_limits_enabled(setting: SchedulingSetting) -> bool:
    return any(
        int(value or 0) > 0
        for value in (
            setting.default_account_cooldown_seconds,
            setting.default_account_hour_limit,
            setting.default_account_day_limit,
        )
    )


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


__all__ = ["capacity_setting", "prime_capacity_cache"]
