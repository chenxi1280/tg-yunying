from __future__ import annotations

import hashlib
import random
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class SearchJoinDailyCapacity:
    effective_date: str
    capacity_day_kind: str
    normal_curve_capacity: int
    strict_hour_ceiling: int
    account_source_capacity: int
    max_source_attempts: int
    strict_planning_capacity: int
    behavior_pacing_unavailable_count: int
    behavior_pacing_unavailable_reasons: tuple[str, ...]
    remaining_executable_hours: int
    occupied_source_count: int
    current_hour_source_occupied: int
    current_hour_available: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "behavior_pacing_unavailable_reasons": list(self.behavior_pacing_unavailable_reasons),
            "source_capacity": self.max_source_attempts,
        }


def first_full_capacity_date(
    timezone_name: str,
    *,
    now_value: datetime,
    scheduled_start: datetime | None,
    scheduled_end: datetime | None,
) -> date | None:
    timezone = ZoneInfo(timezone_name or "Asia/Shanghai")
    local_now = _as_local(now_value, timezone)
    start = _as_local(scheduled_start, timezone) if scheduled_start else local_now
    candidate = start.date()
    if start.time() != time.min or candidate == local_now.date() and local_now.time() != time.min:
        candidate += timedelta(days=1)
    day_start = datetime.combine(candidate, time.min, tzinfo=timezone)
    if scheduled_end and _as_local(scheduled_end, timezone) <= day_start + timedelta(days=1):
        return None
    return candidate


def configured_capacity_window(
    timezone_name: str,
    *,
    now_value: datetime,
    scheduled_start: datetime | None,
    scheduled_end: datetime | None,
) -> tuple[date, str, datetime, datetime] | None:
    timezone = ZoneInfo(timezone_name or "Asia/Shanghai")
    local_now = _as_local(now_value, timezone)
    start = max(local_now, _as_local(scheduled_start, timezone)) if scheduled_start else local_now
    end = _as_local(scheduled_end, timezone) if scheduled_end else start + timedelta(days=366)
    if end <= start:
        return None
    full_date = first_full_capacity_date(
        timezone_name,
        now_value=now_value,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
    )
    if full_date is not None:
        full_start = datetime.combine(full_date, time.min, tzinfo=timezone)
        return full_date, "full_day", full_start, full_start + timedelta(days=1)
    day_end = datetime.combine(start.date() + timedelta(days=1), time.min, tzinfo=timezone)
    return start.date(), "partial_day", start, min(end, day_end)


def strict_daily_capacity(
    task_id: str,
    timezone_name: str,
    config: dict[str, Any],
    *,
    candidate_account_count: int,
    account_source_capacity: int,
    effective_date: date,
    capacity_day_kind: str = "full_day",
    active_start: datetime | None = None,
    active_end: datetime | None = None,
    daily_source_budget: int | None = None,
    occupied_sources_by_hour: dict[int, int] | None = None,
    current_hour: int | None = None,
) -> SearchJoinDailyCapacity:
    curve = _curve(config)
    max_per_hour = max(0, int(config.get("max_actions_per_hour") or 0))
    actions_per_round = max(0, int(config.get("actions_per_round") or 0))
    window_capacities = _window_capacities(
        config,
        curve,
        max_per_hour,
        actions_per_round,
        effective_date=effective_date,
        timezone_name=timezone_name,
        active_start=active_start,
        active_end=active_end,
    )
    normal_capacity = sum(item[1] for item in window_capacities)
    occupied = _occupied_sources(occupied_sources_by_hour)
    remaining_windows = _remaining_windows(window_capacities, occupied)
    strict_ceiling = sum(strict for _, _, strict in remaining_windows)
    behavior_deduction, reasons, behavior_by_hour = _behavior_deduction(
        task_id,
        config,
        effective_date,
        timezone_name,
        remaining_windows,
        occupied,
    )
    executable_hours = sum(
        1
        for hour, _, strict in remaining_windows
        if strict > int(behavior_by_hour.get(hour, 0))
    )
    daily_budget = int(config.get("max_actions_per_day") or 0) if daily_source_budget is None else int(daily_source_budget)
    bounded_daily_budget = daily_budget if daily_source_budget is not None else (daily_budget if daily_budget > 0 else strict_ceiling)
    bounded_accounts = max(0, int(account_source_capacity)) if candidate_account_count > 0 else 0
    max_sources = min(bounded_daily_budget, strict_ceiling, bounded_accounts)
    behavior_available = max(0, strict_ceiling - behavior_deduction)
    strict_capacity = min(max_sources, behavior_available)
    normalized_current_hour = int(current_hour) if current_hour is not None else -1
    current_window_capacity = next(
        (strict for hour, _, strict in remaining_windows if hour == normalized_current_hour),
        0,
    )
    return SearchJoinDailyCapacity(
        effective_date=effective_date.isoformat(),
        capacity_day_kind=capacity_day_kind,
        normal_curve_capacity=min(normal_capacity, bounded_daily_budget, bounded_accounts),
        strict_hour_ceiling=strict_ceiling,
        account_source_capacity=bounded_accounts,
        max_source_attempts=max_sources,
        strict_planning_capacity=strict_capacity,
        behavior_pacing_unavailable_count=behavior_deduction,
        behavior_pacing_unavailable_reasons=tuple(reasons),
        remaining_executable_hours=executable_hours,
        occupied_source_count=sum(occupied.values()),
        current_hour_source_occupied=int(occupied.get(normalized_current_hour, 0)),
        current_hour_available=max(0, current_window_capacity - int(behavior_by_hour.get(normalized_current_hour, 0))),
    )


def configured_account_source_capacity(
    config: dict[str, Any],
    *,
    candidate_account_count: int,
    allow_repeat: bool,
    keyword_count: int,
) -> int:
    daily_budget = int(config.get("max_actions_per_day") or 0)
    if daily_budget <= 0:
        return 0
    if allow_repeat:
        return daily_budget
    account_limit = int(config.get("per_account_daily_action_limit") or 0)
    keyword_limit = int(config.get("per_keyword_account_daily_limit") or 0)
    limits = [limit for limit in (account_limit, keyword_limit * max(1, keyword_count)) if limit > 0]
    per_account = min(limits) if limits else daily_budget
    return min(daily_budget, max(0, int(candidate_account_count)) * per_account)


def _window_capacities(
    config: dict[str, Any],
    curve: list[int],
    max_per_hour: int,
    actions_per_round: int,
    *,
    effective_date: date,
    timezone_name: str,
    active_start: datetime | None,
    active_end: datetime | None,
) -> list[tuple[int, int, int]]:
    capacities: list[tuple[int, int, int]] = []
    for hour, weight in enumerate(curve):
        fraction = _executable_fraction(
            config.get("quiet_hours"),
            hour,
            effective_date=effective_date,
            timezone_name=timezone_name,
            active_start=active_start,
            active_end=active_end,
        )
        normal = int(min(max_per_hour, actions_per_round * weight) * fraction)
        strict = int(max_per_hour * fraction) if weight > 0 else 0
        capacities.append((hour, normal, strict))
    return capacities


def _behavior_deduction(
    task_id: str,
    config: dict[str, Any],
    effective_date: date,
    timezone_name: str,
    windows: list[tuple[int, int, int]],
    occupied_sources_by_hour: dict[int, int],
) -> tuple[int, list[str], dict[int, int]]:
    day_key = effective_date.isoformat()
    if _decision(task_id, "daily", day_key, float(config.get("daily_skip_probability") or 0)):
        deductions = {hour: strict for hour, _, strict in windows if strict > 0}
        return sum(deductions.values()), ["daily_skipped_by_pacing"], deductions
    deduction = 0
    reasons: list[str] = []
    deductions_by_hour: dict[int, int] = {}
    for hour, _, strict in windows:
        if strict <= 0:
            continue
        hour_key = _hour_scope_key(timezone_name, effective_date, hour)
        if _decision(task_id, "hourly", hour_key, float(config.get("hourly_skip_probability") or 0)):
            deduction += strict
            deductions_by_hour[hour] = strict
            reasons.append(f"hourly_skipped_by_pacing:{hour:02d}")
            continue
        skipped = _action_skip_count(
            task_id,
            timezone_name,
            effective_date,
            hour,
            strict,
            int(occupied_sources_by_hour.get(hour, 0)),
            float(config.get("skip_probability_per_action") or 0),
        )
        if skipped:
            deduction += skipped
            deductions_by_hour[hour] = skipped
            reasons.append(f"action_skipped_by_pacing:{hour:02d}:{skipped}")
    return deduction, reasons, deductions_by_hour


def _action_skip_count(
    task_id: str,
    timezone_name: str,
    effective_date: date,
    hour: int,
    slots: int,
    occupied_sources: int,
    probability: float,
) -> int:
    threshold = _probability(probability)
    return sum(
        _seeded_float(
            task_id,
            "action",
            strict_capacity_action_key(timezone_name, effective_date, hour, slot),
        ) < threshold
        for slot in range(max(0, int(occupied_sources)), max(0, int(occupied_sources)) + slots)
    )


def _decision(task_id: str, scope: str, key: str, probability: float) -> bool:
    threshold = _probability(probability)
    return threshold > 0 and _seeded_float(task_id, scope, key) < threshold


def _probability(value: float) -> float:
    return max(0.0, min(1.0, float(value or 0)))


def _seeded_float(task_id: str, scope: str, key: str) -> float:
    seed = hashlib.sha256(f"{task_id}:{scope}:{key}".encode("utf-8")).hexdigest()
    return random.Random(seed).random()


def _curve(config: dict[str, Any]) -> list[int]:
    raw_curve = config.get("hourly_round_curve")
    if not isinstance(raw_curve, list) or len(raw_curve) != 24:
        return [0] * 24
    return [max(0, int(value)) for value in raw_curve]


def strict_capacity_action_key(
    timezone_name: str,
    effective_date: date,
    hour: int,
    slot: int,
) -> str:
    return f"strict:{_hour_scope_key(timezone_name, effective_date, hour)}:{slot}"


def _hour_scope_key(timezone_name: str, effective_date: date, hour: int) -> str:
    timezone = ZoneInfo(timezone_name or "Asia/Shanghai")
    return datetime.combine(effective_date, time(hour), tzinfo=timezone).isoformat()


def _occupied_sources(values: dict[int, int] | None) -> dict[int, int]:
    return {
        int(hour): max(0, int(count))
        for hour, count in (values or {}).items()
        if 0 <= int(hour) <= 23 and int(count) > 0
    }


def _remaining_windows(
    windows: list[tuple[int, int, int]],
    occupied_sources_by_hour: dict[int, int],
) -> list[tuple[int, int, int]]:
    return [
        (hour, normal, max(0, strict - int(occupied_sources_by_hour.get(hour, 0))))
        for hour, normal, strict in windows
    ]


def _executable_fraction(
    raw_quiet_hours: Any,
    hour: int,
    *,
    effective_date: date,
    timezone_name: str,
    active_start: datetime | None,
    active_end: datetime | None,
) -> float:
    start = _clock_minutes(raw_quiet_hours.get("start")) if isinstance(raw_quiet_hours, dict) else None
    end = _clock_minutes(raw_quiet_hours.get("end")) if isinstance(raw_quiet_hours, dict) else None
    timezone = ZoneInfo(timezone_name or "Asia/Shanghai")
    executable = sum(
        _minute_is_executable(
            datetime.combine(effective_date, time(hour, minute), tzinfo=timezone),
            hour * 60 + minute,
            start,
            end,
            active_start,
            active_end,
        )
        for minute in range(60)
    )
    return executable / 60


def _minute_is_executable(
    moment: datetime,
    minute_of_day: int,
    quiet_start: int | None,
    quiet_end: int | None,
    active_start: datetime | None,
    active_end: datetime | None,
) -> bool:
    if active_start and moment < active_start or active_end and moment >= active_end:
        return False
    if quiet_start is None or quiet_end is None or quiet_start == quiet_end:
        return True
    return not _minute_in_quiet(minute_of_day, quiet_start, quiet_end)


def _clock_minutes(value: Any) -> int | None:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return None
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except ValueError:
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def _minute_in_quiet(value: int, start: int, end: int) -> bool:
    return start <= value < end if start < end else value >= start or value < end


def _as_local(value: datetime, timezone: ZoneInfo) -> datetime:
    source = value if value.tzinfo else value.replace(tzinfo=timezone)
    return source.astimezone(timezone)


__all__ = [
    "SearchJoinDailyCapacity",
    "configured_capacity_window",
    "configured_account_source_capacity",
    "first_full_capacity_date",
    "strict_capacity_action_key",
    "strict_daily_capacity",
]
