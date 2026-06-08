from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, MessageTask, SchedulingSetting, TaskStatus, TgAccount
from app.services._common import _now
from app.services.ai_config import get_scheduling_setting


ACTION_OCCUPIED_STATUSES = {"pending", "claiming", "executing", "success", "unknown_after_send"}
MESSAGE_TASK_OCCUPIED_STATUSES = {TaskStatus.QUEUED.value, TaskStatus.SENDING.value, TaskStatus.SENT.value}


@dataclass(frozen=True)
class AccountCapacityDecision:
    available: bool
    defer_until: datetime | None = None
    reason_code: str = ""
    reason: str = ""


@dataclass(frozen=True)
class AccountCapacityReservation:
    account_id: int
    scheduled_at: datetime


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
) -> AccountCapacityDecision:
    setting = get_scheduling_setting(session, tenant_id)
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
) -> list[TgAccount]:
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
) -> int:
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
    action_count = session.scalar(select(func.count(Action.id)).where(*action_filters)) or 0
    message_count = session.scalar(select(func.count(MessageTask.id)).where(*message_filters)) or 0
    return int(action_count) + int(message_count)


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
) -> datetime | None:
    cooldown = int(setting.default_account_cooldown_seconds or 0)
    if cooldown <= 0:
        return None
    last_at = _last_occupied_at(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        scheduled_at=scheduled_at,
        exclude_action_ids=exclude_action_ids,
        exclude_message_task_id=exclude_message_task_id,
    )
    reserved_at = _last_reserved_at(reservations, account_id, scheduled_at)
    if reserved_at and (last_at is None or reserved_at > last_at):
        last_at = reserved_at
    if not last_at:
        return None
    return _naive(last_at) + timedelta(seconds=cooldown)


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
    values = [value for value in [action_at, message_at] if value is not None]
    return max(values) if values else None


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


__all__ = [
    "AccountCapacityDecision",
    "AccountCapacityReservation",
    "account_capacity_decision",
    "available_accounts_by_capacity",
    "defer_until_with_jitter",
    "next_capacity_window",
]
