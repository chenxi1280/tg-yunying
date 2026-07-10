from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SchedulingSetting, Task, TgGroup


def coverage_capacity_proof(
    *,
    group: TgGroup,
    target_account_count: int,
    target_per_account: int,
    max_actions_per_hour: int,
    account_day_limit: int,
    account_hour_limit: int,
    account_cooldown_seconds: int,
) -> dict[str, object]:
    target_accounts = max(0, int(target_account_count or 0))
    per_account = max(1, int(target_per_account or 1))
    required = target_accounts * per_account
    active_seconds = _active_window_seconds(group.active_window)
    active_hours = max(1, math.ceil(active_seconds / 3600))
    capacities = _capacity_dimensions(
        group=group,
        account_count=target_accounts,
        active_seconds=active_seconds,
        active_hours=active_hours,
        max_actions_per_hour=max_actions_per_hour,
        account_day_limit=account_day_limit,
        account_hour_limit=account_hour_limit,
        account_cooldown_seconds=account_cooldown_seconds,
    )
    bounded = [value for value in capacities.values() if value is not None]
    effective = min(bounded) if bounded else required
    blockers = [name for name, value in capacities.items() if value is not None and value < required]
    return {
        "required_daily_messages": required,
        "target_account_count": target_accounts,
        "target_per_account": per_account,
        "active_window_hours": active_hours,
        "capacity_dimensions": capacities,
        "effective_daily_capacity": effective,
        "capacity_gap": max(0, required - effective),
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
) -> dict[str, object]:
    setting = session.scalar(
        select(SchedulingSetting).where(SchedulingSetting.tenant_id == task.tenant_id)
    )
    setting = setting or SchedulingSetting(tenant_id=task.tenant_id)
    return coverage_capacity_proof(
        group=group,
        target_account_count=target_account_count,
        target_per_account=target_per_account,
        max_actions_per_hour=int((task.pacing_config or {}).get("max_actions_per_hour") or 0),
        account_day_limit=int(setting.default_account_day_limit or 0),
        account_hour_limit=int(setting.default_account_hour_limit or 0),
        account_cooldown_seconds=int(setting.default_account_cooldown_seconds or 0),
    )


def _capacity_dimensions(
    *,
    group: TgGroup,
    account_count: int,
    active_seconds: int,
    active_hours: int,
    max_actions_per_hour: int,
    account_day_limit: int,
    account_hour_limit: int,
    account_cooldown_seconds: int,
) -> dict[str, int | None]:
    group_cooldown = max(0, int(group.group_cooldown_seconds or 0))
    return {
        "group_daily_limit": _positive_limit(group.daily_limit),
        "group_cooldown": _window_capacity(active_seconds, group_cooldown),
        "task_hour_limit": _scaled_limit(max_actions_per_hour, active_hours),
        "account_day_limit": _scaled_limit(account_day_limit, account_count),
        "account_hour_limit": _scaled_limit(account_hour_limit, account_count * active_hours),
        "account_cooldown": _scaled_window_capacity(
            active_seconds,
            account_cooldown_seconds,
            account_count,
        ),
    }


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


__all__ = ["coverage_capacity_proof", "task_coverage_capacity_proof"]
