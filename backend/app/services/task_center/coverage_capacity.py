from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, SchedulingSetting, Task, TaskAccountDailyCoverage, TgGroup
from app.services._common import _now

from .daily_coverage_schedule import active_window_bounds


OCCUPIED_ACTION_STATUSES = ("pending", "claiming", "executing", "success", "unknown_after_send")
RESERVED_COVERAGE_STATES = {"reserved", "sending", "unknown"}
PENDING_ACTION_STATUSES = ("pending", "claiming", "executing")
HARD_HOURLY_WINDOW_SECONDS = 60 * 60
HARD_HOURLY_GROUP_COOLDOWN_BLOCKER_CODE = "hard_hourly_group_cooldown_insufficient"
HARD_HOURLY_GROUP_COOLDOWN_BLOCKED_MESSAGE = "硬小时目标超过群冷却容量，已停止创建会过期的 Action"


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
    pending_group_actions: int = 0,
    pending_task_actions: int = 0,
    now: datetime | None = None,
) -> dict[str, object]:
    target_accounts = max(0, int(target_account_count or 0))
    per_account = max(1, int(target_per_account or 1))
    required = target_accounts * per_account
    confirmed = min(required, max(0, int(confirmed_message_count or 0)))
    reserved = min(required - confirmed, max(0, int(reserved_message_count or 0)))
    remaining_required = required - confirmed - reserved
    active_seconds = _available_active_window_seconds(group.active_window, now)
    active_hours = math.ceil(active_seconds / 3600)
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
        pending_group_actions=pending_group_actions,
        pending_task_actions=pending_task_actions,
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
        "remaining_active_window_seconds": active_seconds,
        "capacity_dimensions": capacities,
        "effective_daily_capacity": effective,
        "capacity_gap": max(0, remaining_required - effective),
        "sufficient": not blockers,
        "blockers": blockers,
        "blocker_code": "" if not blockers else "daily_coverage_capacity_insufficient",
    }


def hard_hourly_required_hourly_messages(
    *,
    hourly_target: int,
    backfill_planning_deficit: int = 0,
    required_hourly_messages: int | None = None,
) -> int:
    """Single-hour send pressure used by the group-cooldown gate.

    Historical backfill debt must not be required inside one hour. Match planner
    batching: goal + min(goal, backfill_planning_deficit). Explicit
    required_hourly_messages is capped by that planning rate when provided.
    """
    target = max(0, int(hourly_target or 0))
    backfill = max(0, int(backfill_planning_deficit or 0))
    planning_rate = target + min(target, backfill) if target > 0 else backfill
    if required_hourly_messages is None:
        return planning_rate
    try:
        explicit = max(0, int(required_hourly_messages))
    except (TypeError, ValueError):
        explicit = 0
    if planning_rate <= 0:
        return max(target, explicit)
    return min(max(target, explicit), planning_rate)


def hard_hourly_group_cooldown_proof(
    *,
    group: TgGroup,
    hourly_target: int,
    required_hourly_messages: int | None = None,
    backfill_planning_deficit: int = 0,
) -> dict[str, object]:
    target = max(0, int(hourly_target or 0))
    required = hard_hourly_required_hourly_messages(
        hourly_target=target,
        backfill_planning_deficit=backfill_planning_deficit,
        required_hourly_messages=required_hourly_messages,
    )
    cooldown = max(0, int(group.group_cooldown_seconds or 0))
    capacity = _hard_hourly_cooldown_capacity(cooldown)
    gap = max(0, required - capacity) if capacity is not None else 0
    sufficient = capacity is None or capacity >= required
    return {
        "hourly_target": target,
        "required_hourly_messages": required,
        "backfill_planning_deficit": max(0, int(backfill_planning_deficit or 0)),
        "group_cooldown_seconds": cooldown,
        "group_cooldown_hourly_capacity": capacity,
        "capacity_gap": gap,
        "recommended_max_group_cooldown_seconds": _recommended_hard_hourly_cooldown(required),
        "sufficient": sufficient,
        "blocker_code": "" if sufficient else HARD_HOURLY_GROUP_COOLDOWN_BLOCKER_CODE,
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
    now: datetime | None = None,
) -> dict[str, object]:
    now_value = now or _naive_now()
    setting = session.scalar(
        select(SchedulingSetting).where(SchedulingSetting.tenant_id == task.tenant_id)
    )
    setting = setting or SchedulingSetting(tenant_id=task.tenant_id)
    group_occupied, task_occupied = _occupied_actions(session, task, group)
    pending_group, pending_task = _pending_action_commitments(session, task, group, now=now_value)
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
        daily_task_capacity=_task_daily_schedule_capacity(task, group, now=now_value),
        occupied_group_actions=group_occupied,
        occupied_task_actions=task_occupied,
        pending_group_actions=pending_group,
        pending_task_actions=pending_task,
        now=now_value,
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
    pending_group_actions: int,
    pending_task_actions: int,
) -> dict[str, int | None]:
    group_cooldown = max(0, int(group.group_cooldown_seconds or 0))
    return {
        "group_daily_limit": _remaining_limit(group.daily_limit, occupied_group_actions),
        "group_cooldown": _remaining_capacity(
            _window_capacity(active_seconds, group_cooldown), pending_group_actions,
        ),
        "task_schedule": _remaining_task_capacity(
            daily_task_capacity, max_actions_per_hour, active_hours, pending_task_actions,
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


def _pending_action_commitments(
    session: Session,
    task: Task,
    group: TgGroup,
    *,
    now: datetime,
) -> tuple[int, int]:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    filters = [
        Action.tenant_id == task.tenant_id,
        Action.action_type == "send_message",
        Action.status.in_(PENDING_ACTION_STATUSES),
        Action.scheduled_at >= day_start,
        Action.scheduled_at < day_end,
    ]
    group_filter = Action.payload["group_id"].as_integer() == group.id
    group_count = session.scalar(select(func.count(Action.id)).where(*filters, group_filter)) or 0
    task_count = session.scalar(select(func.count(Action.id)).where(*filters, Action.task_id == task.id)) or 0
    return int(group_count), int(task_count)


def _task_daily_schedule_capacity(task: Task, group: TgGroup, *, now: datetime | None = None) -> int | None:
    pacing = task.pacing_config or {}
    return daily_task_schedule_capacity(pacing, _messages_per_round(task), group, now=now)


def daily_task_schedule_capacity(
    pacing: dict,
    messages_per_round: int,
    group: TgGroup,
    *,
    now: datetime | None = None,
) -> int | None:
    curve = _hourly_round_curve(pacing)
    max_per_hour = _positive_limit(int(pacing.get("max_actions_per_hour") or 0))
    messages = max(1, int(messages_per_round or 1))
    if curve:
        if now is not None:
            return _remaining_curve_capacity(curve, messages, max_per_hour, group, now)
        return sum(_hour_capacity(rounds, messages, max_per_hour) for rounds in _active_rounds(curve, group))
    if max_per_hour is None:
        return None
    active_seconds = _available_active_window_seconds(group.active_window, now)
    return max_per_hour * math.ceil(active_seconds / 3600)


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


def _available_active_window_seconds(active_window: str, now: datetime | None) -> int:
    if now is None:
        return _active_window_seconds(active_window)
    bounds = _remaining_active_window_bounds(active_window, now)
    if bounds is None:
        return 0
    start, end = bounds
    return max(0, int((end - start).total_seconds()))


def _remaining_active_window_bounds(
    active_window: str,
    now: datetime,
) -> tuple[datetime, datetime] | None:
    previous_start, previous_end = active_window_bounds(active_window, now.date() - timedelta(days=1))
    current_start, current_end = active_window_bounds(active_window, now.date())
    if previous_start <= now < previous_end:
        return now, previous_end
    if now < current_start:
        return current_start, current_end
    if now < current_end:
        return now, current_end
    return None


def _remaining_curve_capacity(
    curve: list[int],
    messages: int,
    max_per_hour: int | None,
    group: TgGroup,
    now: datetime,
) -> int:
    bounds = _remaining_active_window_bounds(group.active_window, now)
    if bounds is None:
        return 0
    start, end = bounds
    cursor = start.replace(minute=0, second=0, microsecond=0)
    capacity = 0
    while cursor < end:
        next_hour = cursor + timedelta(hours=1)
        overlap_start = max(cursor, start)
        overlap_end = min(next_hour, end)
        overlap_seconds = max(0, int((overlap_end - overlap_start).total_seconds()))
        hourly_capacity = _hour_capacity(curve[cursor.hour], messages, max_per_hour)
        capacity += math.floor(hourly_capacity * overlap_seconds / 3600)
        cursor = next_hour
    return capacity


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


def _remaining_capacity(capacity: int | None, occupied: int) -> int | None:
    if capacity is None:
        return None
    return max(0, int(capacity) - max(0, int(occupied)))


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
    if window_seconds <= 0:
        return 0
    if cooldown_seconds <= 0:
        return None
    return max(1, window_seconds // cooldown_seconds + 1)


def _hard_hourly_cooldown_capacity(cooldown_seconds: int) -> int | None:
    if cooldown_seconds <= 0:
        return None
    return HARD_HOURLY_WINDOW_SECONDS // cooldown_seconds


def _recommended_hard_hourly_cooldown(hourly_target: int) -> int | None:
    if hourly_target <= 0:
        return None
    return max(1, HARD_HOURLY_WINDOW_SECONDS // hourly_target)


def _scaled_window_capacity(window_seconds: int, cooldown_seconds: int, multiplier: int) -> int | None:
    capacity = _window_capacity(window_seconds, max(0, int(cooldown_seconds or 0)))
    return capacity * max(0, multiplier) if capacity is not None else None


__all__ = [
    "coverage_capacity_proof",
    "daily_task_schedule_capacity",
    "HARD_HOURLY_GROUP_COOLDOWN_BLOCKED_MESSAGE",
    "HARD_HOURLY_GROUP_COOLDOWN_BLOCKER_CODE",
    "hard_hourly_group_cooldown_proof",
    "hard_hourly_required_hourly_messages",
    "reserved_coverage_message_count",
    "task_coverage_capacity_proof",
]
