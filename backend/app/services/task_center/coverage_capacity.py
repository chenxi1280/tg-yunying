from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, SchedulingSetting, Task, TaskAccountDailyCoverage, TgGroup
from app.services._common import _now


OCCUPIED_ACTION_STATUSES = ("pending", "claiming", "executing", "success", "unknown_after_send")
RESERVED_COVERAGE_STATES = {"reserved", "sending", "unknown"}


def coverage_capacity_proof(
    *,
    group: TgGroup,
    target_account_count: int,
    target_per_account: int,
    max_actions_per_hour: int,
    account_day_limit: int,
    account_hour_limit: int,
    account_cooldown_seconds: int,
    confirmed_message_count: int = 0,
    reserved_message_count: int = 0,
    daily_task_capacity: int | None = None,
    occupied_group_actions: int = 0,
    occupied_task_actions: int = 0,
) -> dict[str, object]:
    target_accounts = max(0, int(target_account_count or 0))
    per_account = max(1, int(target_per_account or 1))
    required = target_accounts * per_account
    confirmed = min(required, max(0, int(confirmed_message_count or 0)))
    reserved = min(required - confirmed, max(0, int(reserved_message_count or 0)))
    remaining_required = required - confirmed - reserved
    active_seconds = _active_window_seconds(group.active_window)
    active_hours = max(1, math.ceil(active_seconds / 3600))
    capacities = _capacity_dimensions(
        group=group,
        account_count=target_accounts,
        active_seconds=active_seconds,
        active_hours=active_hours,
        max_actions_per_hour=max_actions_per_hour,
        daily_task_capacity=daily_task_capacity,
        occupied_group_actions=occupied_group_actions,
        occupied_task_actions=occupied_task_actions,
        account_day_limit=account_day_limit,
        account_hour_limit=account_hour_limit,
        account_cooldown_seconds=account_cooldown_seconds,
    )
    bounded = [value for value in capacities.values() if value is not None]
    effective = min(bounded) if bounded else required
    blockers = [
        name
        for name, value in capacities.items()
        if value is not None and value < remaining_required
    ]
    return {
        "required_daily_messages": required,
        "remaining_required_messages": remaining_required,
        "reserved_messages": reserved,
        "target_account_count": target_accounts,
        "target_per_account": per_account,
        "active_window_hours": active_hours,
        "capacity_dimensions": capacities,
        "effective_daily_capacity": effective,
        "capacity_gap": max(0, remaining_required - effective),
        "sufficient": not blockers,
        "blockers": blockers,
        "blocker_code": "" if not blockers else "daily_coverage_capacity_insufficient",
    }


def task_coverage_capacity_proof(
    session: Session,
    task: Task,
    group: TgGroup,
    *,
    target_account_count: int,
    target_per_account: int,
    confirmed_message_count: int = 0,
    reserved_message_count: int = 0,
) -> dict[str, object]:
    setting = session.scalar(
        select(SchedulingSetting).where(SchedulingSetting.tenant_id == task.tenant_id)
    )
    setting = setting or SchedulingSetting(tenant_id=task.tenant_id)
    group_occupied, task_occupied = _occupied_actions(session, task, group)
    return coverage_capacity_proof(
        group=group,
        target_account_count=target_account_count,
        target_per_account=target_per_account,
        confirmed_message_count=confirmed_message_count,
        reserved_message_count=reserved_message_count,
        max_actions_per_hour=int((task.pacing_config or {}).get("max_actions_per_hour") or 0),
        account_day_limit=int(setting.default_account_day_limit or 0),
        account_hour_limit=int(setting.default_account_hour_limit or 0),
        account_cooldown_seconds=int(setting.default_account_cooldown_seconds or 0),
        daily_task_capacity=_task_daily_schedule_capacity(task, group),
        occupied_group_actions=group_occupied,
        occupied_task_actions=task_occupied,
    )


def _capacity_dimensions(
    *,
    group: TgGroup,
    account_count: int,
    active_seconds: int,
    active_hours: int,
    max_actions_per_hour: int,
    daily_task_capacity: int | None,
    occupied_group_actions: int,
    occupied_task_actions: int,
    account_day_limit: int,
    account_hour_limit: int,
    account_cooldown_seconds: int,
) -> dict[str, int | None]:
    group_cooldown = max(0, int(group.group_cooldown_seconds or 0))
    return {
        "group_daily_limit": _remaining_limit(group.daily_limit, occupied_group_actions),
        "group_cooldown": _window_capacity(active_seconds, group_cooldown),
        "task_schedule": _remaining_task_capacity(
            daily_task_capacity, max_actions_per_hour, active_hours, occupied_task_actions,
        ),
        "account_day_limit": _scaled_limit(account_day_limit, account_count),
        "account_hour_limit": _scaled_limit(account_hour_limit, account_count * active_hours),
        "account_cooldown": _scaled_window_capacity(
            active_seconds,
            account_cooldown_seconds,
            account_count,
        ),
    }


def reserved_coverage_message_count(rows: list[TaskAccountDailyCoverage]) -> int:
    return sum(
        min(1, max(0, int(row.target_count or 1) - int(row.confirmed_count or 0)))
        for row in rows
        if row.state in RESERVED_COVERAGE_STATES
    )


def _occupied_actions(session: Session, task: Task, group: TgGroup) -> tuple[int, int]:
    now = _naive_now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    occupied_at = func.coalesce(Action.executed_at, Action.scheduled_at)
    filters = [
        Action.tenant_id == task.tenant_id,
        Action.action_type == "send_message",
        Action.status.in_(OCCUPIED_ACTION_STATUSES),
        occupied_at >= day_start,
        occupied_at < day_end,
    ]
    group_filter = Action.payload["group_id"].as_integer() == group.id
    group_count = session.scalar(select(func.count(Action.id)).where(*filters, group_filter)) or 0
    task_count = session.scalar(select(func.count(Action.id)).where(*filters, Action.task_id == task.id)) or 0
    return int(group_count), int(task_count)


def _task_daily_schedule_capacity(task: Task, group: TgGroup) -> int | None:
    pacing = task.pacing_config or {}
    return daily_task_schedule_capacity(pacing, _messages_per_round(task), group)


def daily_task_schedule_capacity(
    pacing: dict,
    messages_per_round: int,
    group: TgGroup,
) -> int | None:
    curve = _hourly_round_curve(pacing)
    max_per_hour = _positive_limit(int(pacing.get("max_actions_per_hour") or 0))
    messages = max(1, int(messages_per_round or 1))
    if curve:
        return sum(_hour_capacity(rounds, messages, max_per_hour) for rounds in _active_rounds(curve, group))
    if max_per_hour is None:
        return None
    return max_per_hour * max(1, math.ceil(_active_window_seconds(group.active_window) / 3600))


def _hourly_round_curve(pacing: dict) -> list[int]:
    profile = pacing.get("operation_profile") or {}
    raw = profile.get("hourly_activity_curve") if isinstance(profile, dict) else None
    if not isinstance(raw, list) or len(raw) != 24:
        return []
    try:
        return [max(0, int(value)) for value in raw]
    except (TypeError, ValueError):
        return []


def _active_rounds(curve: list[int], group: TgGroup) -> list[int]:
    start, end = _active_hour_range(group.active_window)
    return [curve[hour % 24] for hour in range(start, end)]


def _active_hour_range(active_window: str) -> tuple[int, int]:
    start_raw, end_raw = str(active_window or "09:00-23:00").split("-", 1)
    start_minutes = _minute_of_day(start_raw)
    end_minutes = _minute_of_day(end_raw)
    start_hour = start_minutes // 60
    end_hour = math.ceil(end_minutes / 60) or 24
    return start_hour, end_hour if end_minutes > start_minutes else end_hour + 24


def _messages_per_round(task: Task) -> int:
    config = task.type_config or {}
    return max(1, int(config.get("messages_per_round") or 1))


def _hour_capacity(rounds: int, messages: int, max_per_hour: int | None) -> int:
    raw = max(0, rounds) * messages
    return min(raw, max_per_hour) if max_per_hour is not None else raw


def _active_window_seconds(active_window: str) -> int:
    try:
        start_raw, end_raw = str(active_window or "09:00-23:00").split("-", 1)
        start = _minute_of_day(start_raw)
        end = _minute_of_day(end_raw)
    except (TypeError, ValueError):
        raise ValueError(f"invalid group active window: {active_window}")
    minutes = end - start if end > start else (24 * 60 - start + end)
    return max(60, minutes * 60)


def _minute_of_day(value: str) -> int:
    hour, minute = value.strip().split(":", 1)
    parsed_hour = int(hour)
    parsed_minute = int(minute)
    if not 0 <= parsed_hour <= 23 or not 0 <= parsed_minute <= 59:
        raise ValueError("invalid time")
    return parsed_hour * 60 + parsed_minute


def _positive_limit(value: int) -> int | None:
    parsed = int(value or 0)
    return parsed if parsed > 0 else None


def _remaining_limit(value: int, occupied: int) -> int | None:
    limit = _positive_limit(value)
    return max(0, limit - max(0, int(occupied))) if limit is not None else None


def _remaining_task_capacity(
    daily_task_capacity: int | None,
    max_actions_per_hour: int,
    active_hours: int,
    occupied_task_actions: int,
) -> int | None:
    capacity = daily_task_capacity
    if capacity is None:
        capacity = _scaled_limit(max_actions_per_hour, active_hours)
    return max(0, capacity - max(0, int(occupied_task_actions))) if capacity is not None else None


def _naive_now() -> datetime:
    return _now().replace(tzinfo=None)


def _scaled_limit(value: int, multiplier: int) -> int | None:
    limit = _positive_limit(value)
    return limit * max(0, multiplier) if limit is not None else None


def _window_capacity(window_seconds: int, cooldown_seconds: int) -> int | None:
    if cooldown_seconds <= 0:
        return None
    return max(1, window_seconds // cooldown_seconds + 1)


def _scaled_window_capacity(window_seconds: int, cooldown_seconds: int, multiplier: int) -> int | None:
    capacity = _window_capacity(window_seconds, max(0, int(cooldown_seconds or 0)))
    return capacity * max(0, multiplier) if capacity is not None else None


__all__ = [
    "coverage_capacity_proof",
    "daily_task_schedule_capacity",
    "reserved_coverage_message_count",
    "task_coverage_capacity_proof",
]
