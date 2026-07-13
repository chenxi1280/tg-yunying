from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, MessageTask, SchedulingSetting, TaskStatus, TgAccount
from app.services._common import _now
from app.services import account_capacity_timeline
from app.services.account_capacity_bulk import capacity_setting, prime_capacity_cache
from app.services.account_capacity_types import AccountCapacityCache, AccountCapacityDecision, AccountCapacityReservation


ACTION_OCCUPIED_STATUSES = {"pending", "claiming", "executing", "success", "unknown_after_send"}
MESSAGE_TASK_OCCUPIED_STATUSES = {TaskStatus.QUEUED.value, TaskStatus.SENDING.value, TaskStatus.SENT.value}


def account_capacity_decision(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    scheduled_at: datetime | None = None,
    exclude_action_id: str | None = None,
    exclude_action_ids: set[str] | None = None,
    exclude_message_task_id: int | None = None,
    reservations: list[AccountCapacityReservation] | None = None,
    cache: AccountCapacityCache | None = None,
) -> AccountCapacityDecision:
    setting = capacity_setting(session, tenant_id, cache)
    at = _naive(scheduled_at or _now())
    reserved = reservations or []
    excluded_actions = _excluded_action_ids(exclude_action_id, exclude_action_ids)
    candidates: list[tuple[datetime, str, str]] = []

    cooldown_until = _cooldown_until(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        setting=setting,
        scheduled_at=at,
        exclude_action_ids=excluded_actions,
        exclude_message_task_id=exclude_message_task_id,
        reservations=reserved,
        cache=cache,
    )
    if cooldown_until and cooldown_until > at:
        candidates.append((cooldown_until, "account_cooldown", f"账号全局冷却中，延后至 {cooldown_until:%Y-%m-%d %H:%M:%S}"))

    hour_limit = int(setting.default_account_hour_limit or 0)
    if hour_limit > 0:
        hour_start = at.replace(minute=0, second=0, microsecond=0)
        hour_end = hour_start + timedelta(hours=1)
        occupied = _occupied_count(
            session,
            tenant_id,
            account_id,
            hour_start,
            hour_end,
            excluded_actions,
            exclude_message_task_id,
            cache,
        )
        if occupied + _reserved_count(reserved, account_id, hour_start, hour_end) >= hour_limit:
            candidates.append((hour_end, "account_hour_limit", f"账号每小时发送/互动已达上限 {hour_limit}"))

    day_limit = int(setting.default_account_day_limit or 0)
    if day_limit > 0:
        day_start = at.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        occupied = _occupied_count(
            session,
            tenant_id,
            account_id,
            day_start,
            day_end,
            excluded_actions,
            exclude_message_task_id,
            cache,
        )
        if occupied + _reserved_count(reserved, account_id, day_start, day_end) >= day_limit:
            candidates.append((day_end, "account_day_limit", f"账号每日发送/互动已达上限 {day_limit}"))

    if not candidates:
        return AccountCapacityDecision(available=True)
    defer_until, reason_code, reason = max(candidates, key=lambda item: item[0])
    return AccountCapacityDecision(False, defer_until_with_jitter(setting, defer_until), reason_code, reason)


def available_accounts_by_capacity(
    session: Session,
    *,
    tenant_id: int,
    accounts: list[TgAccount],
    scheduled_at: datetime | None = None,
    limit: int | None = None,
    exclude_action_id: str | None = None,
    exclude_action_ids: set[str] | None = None,
    exclude_message_task_id: int | None = None,
    reservations: list[AccountCapacityReservation] | None = None,
    cache: AccountCapacityCache | None = None,
) -> list[TgAccount]:
    at = _naive(scheduled_at or _now())
    excluded_actions = _excluded_action_ids(exclude_action_id, exclude_action_ids)
    if cache and accounts:
        prime_capacity_cache(
            session,
            tenant_id=tenant_id,
            account_ids=[account.id for account in accounts],
            scheduled_at=at,
            setting=capacity_setting(session, tenant_id, cache),
            exclude_action_ids=excluded_actions,
            exclude_message_task_id=exclude_message_task_id,
            action_statuses=ACTION_OCCUPIED_STATUSES,
            message_statuses=MESSAGE_TASK_OCCUPIED_STATUSES,
            cache=cache,
        )
    available: list[TgAccount] = []
    for account in accounts:
        decision = account_capacity_decision(
            session,
            tenant_id=tenant_id,
            account_id=account.id,
            scheduled_at=scheduled_at,
            exclude_action_id=exclude_action_id,
            exclude_action_ids=exclude_action_ids,
            exclude_message_task_id=exclude_message_task_id,
            reservations=reservations,
            cache=cache,
        )
        if decision.available:
            available.append(account)
            if limit and len(available) >= limit:
                break
    return available


def next_capacity_window(
    session: Session,
    *,
    tenant_id: int,
    account_ids: list[int],
    scheduled_at: datetime | None = None,
    exclude_action_id: str | None = None,
    exclude_action_ids: set[str] | None = None,
    exclude_message_task_id: int | None = None,
    reservations: list[AccountCapacityReservation] | None = None,
    cache: AccountCapacityCache | None = None,
) -> AccountCapacityDecision:
    decisions = [
        account_capacity_decision(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            scheduled_at=scheduled_at,
            exclude_action_id=exclude_action_id,
            exclude_action_ids=exclude_action_ids,
            exclude_message_task_id=exclude_message_task_id,
            reservations=reservations,
            cache=cache,
        )
        for account_id in account_ids
    ]
    blocked = [decision for decision in decisions if not decision.available and decision.defer_until is not None]
    if not blocked:
        return AccountCapacityDecision(True)
    return min(blocked, key=lambda item: item.defer_until or _now())


def defer_until_with_jitter(setting: SchedulingSetting, base: datetime) -> datetime:
    jitter_min = max(0, int(setting.jitter_min_seconds or 0))
    jitter_max = max(jitter_min, int(setting.jitter_max_seconds or jitter_min))
    jitter = random.randint(jitter_min, jitter_max) if jitter_max else 0
    return _naive(base) + timedelta(seconds=jitter)


def _occupied_count(
    session: Session,
    tenant_id: int,
    account_id: int,
    start: datetime,
    end: datetime,
    exclude_action_ids: set[str],
    exclude_message_task_id: int | None,
    cache: AccountCapacityCache | None,
) -> int:
    cache_key = (
        tenant_id,
        account_id,
        start,
        end,
        tuple(sorted(exclude_action_ids)),
        exclude_message_task_id or 0,
    )
    if cache and cache_key in cache.occupied_counts:
        return cache.occupied_counts[cache_key]
    action_occupied_at = func.coalesce(Action.executed_at, Action.scheduled_at)
    action_filters = [
        Action.tenant_id == tenant_id,
        Action.account_id == account_id,
        Action.status.in_(ACTION_OCCUPIED_STATUSES),
        action_occupied_at >= start,
        action_occupied_at < end,
    ]
    if exclude_action_ids:
        action_filters.append(Action.id.not_in(exclude_action_ids))
    message_account_id = func.coalesce(MessageTask.account_id, MessageTask.preferred_account_id)
    message_occupied_at = func.coalesce(MessageTask.sent_at, MessageTask.scheduled_at)
    message_filters = [
        MessageTask.tenant_id == tenant_id,
        message_account_id == account_id,
        MessageTask.status.in_(MESSAGE_TASK_OCCUPIED_STATUSES),
        message_occupied_at >= start,
        message_occupied_at < end,
    ]
    if exclude_message_task_id:
        message_filters.append(MessageTask.id != exclude_message_task_id)
    action_count = session.scalar(select(func.count()).select_from(Action).where(*action_filters)) or 0
    message_count = session.scalar(select(func.count()).select_from(MessageTask).where(*message_filters)) or 0
    total = int(action_count) + int(message_count)
    if cache:
        cache.occupied_counts[cache_key] = total
    return total


def _cooldown_until(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    setting: SchedulingSetting,
    scheduled_at: datetime,
    exclude_action_ids: set[str],
    exclude_message_task_id: int | None,
    reservations: list[AccountCapacityReservation],
    cache: AccountCapacityCache | None = None,
) -> datetime | None:
    cooldown = int(setting.default_account_cooldown_seconds or 0)
    if cooldown <= 0:
        return None
    last_at = _cached_last_occupied_at(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        scheduled_at=scheduled_at,
        cooldown_seconds=cooldown,
        exclude_action_ids=exclude_action_ids,
        exclude_message_task_id=exclude_message_task_id,
        cache=cache,
    )
    reserved_at = _last_reserved_at(reservations, account_id, scheduled_at)
    last_at = _naive(last_at) if last_at else None
    if reserved_at and (last_at is None or reserved_at > last_at):
        last_at = reserved_at
    candidates: list[datetime] = []
    if last_at:
        candidates.append(_naive(last_at) + timedelta(seconds=cooldown))
    future_at = _cached_next_occupied_at(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        scheduled_at=scheduled_at,
        cooldown_seconds=cooldown,
        exclude_action_ids=exclude_action_ids,
        exclude_message_task_id=exclude_message_task_id,
        cache=cache,
    )
    reserved_future_at = _next_reserved_at(reservations, account_id, scheduled_at, cooldown)
    if reserved_future_at and (future_at is None or reserved_future_at < future_at):
        future_at = reserved_future_at
    if future_at:
        candidates.append(_naive(future_at) + timedelta(seconds=cooldown))
    return max(candidates) if candidates else None


def _cached_last_occupied_at(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    scheduled_at: datetime,
    cooldown_seconds: int,
    exclude_action_ids: set[str],
    exclude_message_task_id: int | None,
    cache: AccountCapacityCache | None,
) -> datetime | None:
    cache_key = _capacity_cache_key(tenant_id, account_id, scheduled_at, exclude_action_ids, exclude_message_task_id)
    if cache and cache_key in cache.last_occupied_at:
        return cache.last_occupied_at[cache_key]
    if cache:
        result = account_capacity_timeline.last_occupied_at_from_timeline(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            scheduled_at=scheduled_at,
            cooldown_seconds=cooldown_seconds,
            exclude_action_ids=exclude_action_ids,
            exclude_message_task_id=exclude_message_task_id,
            cache=cache,
            action_statuses=ACTION_OCCUPIED_STATUSES,
            message_statuses=MESSAGE_TASK_OCCUPIED_STATUSES,
        )
    else:
        result = _last_occupied_at(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            scheduled_at=scheduled_at,
            exclude_action_ids=exclude_action_ids,
            exclude_message_task_id=exclude_message_task_id,
        )
    if cache:
        cache.last_occupied_at[cache_key] = result
    return result


def _cached_next_occupied_at(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    scheduled_at: datetime,
    cooldown_seconds: int,
    exclude_action_ids: set[str],
    exclude_message_task_id: int | None,
    cache: AccountCapacityCache | None,
) -> datetime | None:
    window_end = scheduled_at + timedelta(seconds=cooldown_seconds)
    if cache:
        return account_capacity_timeline.next_occupied_at_from_timeline(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            scheduled_at=scheduled_at,
            cooldown_seconds=cooldown_seconds,
            exclude_action_ids=exclude_action_ids,
            exclude_message_task_id=exclude_message_task_id,
            cache=cache,
            action_statuses=ACTION_OCCUPIED_STATUSES,
            message_statuses=MESSAGE_TASK_OCCUPIED_STATUSES,
        )
    return _next_occupied_at(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        scheduled_at=scheduled_at,
        cooldown_seconds=cooldown_seconds,
        exclude_action_ids=exclude_action_ids,
        exclude_message_task_id=exclude_message_task_id,
    )


def _capacity_cache_key(
    tenant_id: int,
    account_id: int,
    scheduled_at: datetime,
    exclude_action_ids: set[str],
    exclude_message_task_id: int | None,
    end_at: datetime | None = None,
) -> tuple:
    return (
        tenant_id,
        account_id,
        scheduled_at,
        end_at,
        tuple(sorted(exclude_action_ids)),
        exclude_message_task_id or 0,
    )


def _last_occupied_at(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    scheduled_at: datetime,
    exclude_action_ids: set[str],
    exclude_message_task_id: int | None,
) -> datetime | None:
    action_filters = [
        Action.tenant_id == tenant_id,
        Action.account_id == account_id,
        Action.status.in_(ACTION_OCCUPIED_STATUSES),
        Action.scheduled_at <= scheduled_at,
    ]
    if exclude_action_ids:
        action_filters.append(Action.id.not_in(exclude_action_ids))
    action_at = session.scalar(
        select(func.max(func.coalesce(Action.executed_at, Action.scheduled_at))).where(*action_filters)
    )
    message_account_id = func.coalesce(MessageTask.account_id, MessageTask.preferred_account_id)
    message_filters = [
        MessageTask.tenant_id == tenant_id,
        message_account_id == account_id,
        MessageTask.status.in_(MESSAGE_TASK_OCCUPIED_STATUSES),
        MessageTask.scheduled_at <= scheduled_at,
    ]
    if exclude_message_task_id:
        message_filters.append(MessageTask.id != exclude_message_task_id)
    message_at = session.scalar(
        select(func.max(func.coalesce(MessageTask.sent_at, MessageTask.scheduled_at))).where(*message_filters)
    )
    values = [_naive(value) for value in [action_at, message_at] if value is not None]
    return max(values) if values else None


def _next_occupied_at(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    scheduled_at: datetime,
    cooldown_seconds: int,
    exclude_action_ids: set[str],
    exclude_message_task_id: int | None,
) -> datetime | None:
    window_end = scheduled_at + timedelta(seconds=cooldown_seconds)
    action_occupied_at = func.coalesce(Action.executed_at, Action.scheduled_at)
    action_filters = [
        Action.tenant_id == tenant_id,
        Action.account_id == account_id,
        Action.status.in_(ACTION_OCCUPIED_STATUSES),
        action_occupied_at > scheduled_at,
        action_occupied_at < window_end,
    ]
    if exclude_action_ids:
        action_filters.append(Action.id.not_in(exclude_action_ids))
    action_at = session.scalar(select(func.min(action_occupied_at)).where(*action_filters))
    message_account_id = func.coalesce(MessageTask.account_id, MessageTask.preferred_account_id)
    message_occupied_at = func.coalesce(MessageTask.sent_at, MessageTask.scheduled_at)
    message_filters = [
        MessageTask.tenant_id == tenant_id,
        message_account_id == account_id,
        MessageTask.status.in_(MESSAGE_TASK_OCCUPIED_STATUSES),
        message_occupied_at > scheduled_at,
        message_occupied_at < window_end,
    ]
    if exclude_message_task_id:
        message_filters.append(MessageTask.id != exclude_message_task_id)
    message_at = session.scalar(select(func.min(message_occupied_at)).where(*message_filters))
    values = [_naive(value) for value in [action_at, message_at] if value is not None]
    return min(values) if values else None


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _excluded_action_ids(exclude_action_id: str | None, exclude_action_ids: set[str] | None) -> set[str]:
    excluded = set(exclude_action_ids or set())
    if exclude_action_id:
        excluded.add(exclude_action_id)
    return excluded


def _reserved_count(
    reservations: list[AccountCapacityReservation],
    account_id: int,
    start: datetime,
    end: datetime,
) -> int:
    return sum(
        1
        for reservation in reservations
        if reservation.account_id == account_id and start <= _naive(reservation.scheduled_at) < end
    )


def _last_reserved_at(
    reservations: list[AccountCapacityReservation],
    account_id: int,
    scheduled_at: datetime,
) -> datetime | None:
    values = [
        _naive(reservation.scheduled_at)
        for reservation in reservations
        if reservation.account_id == account_id and _naive(reservation.scheduled_at) <= scheduled_at
    ]
    return max(values) if values else None
def _next_reserved_at(
    reservations: list[AccountCapacityReservation],
    account_id: int,
    scheduled_at: datetime,
    cooldown_seconds: int,
) -> datetime | None:
    window_end = scheduled_at + timedelta(seconds=cooldown_seconds)
    values = [
        _naive(reservation.scheduled_at)
        for reservation in reservations
        if reservation.account_id == account_id and scheduled_at < _naive(reservation.scheduled_at) < window_end
    ]
    return min(values) if values else None
