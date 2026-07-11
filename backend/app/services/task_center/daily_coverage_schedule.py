from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

from app.models import Task, TaskAccountDailyCoverage, TgGroup


def daily_coverage_due_debt(
    task: Task,
    group: TgGroup,
    rows: list[TaskAccountDailyCoverage],
    *,
    now: datetime,
) -> int:
    if not rows:
        return 0
    required = sum(max(1, int(row.target_count or 1)) for row in rows)
    due = _cumulative_due(task, group, rows[0].coverage_date, required, now)
    confirmed = sum(
        min(max(1, int(row.target_count or 1)), max(0, int(row.confirmed_count or 0)))
        for row in rows
    )
    reserved = sum(
        1
        for row in rows
        if row.state in {"reserved", "sending"}
        and int(row.confirmed_count or 0) < max(1, int(row.target_count or 1))
    )
    return max(0, due - confirmed - reserved)


def _cumulative_due(
    task: Task,
    group: TgGroup,
    coverage_date: date,
    required: int,
    now: datetime,
) -> int:
    start, end = active_window_bounds(group.active_window, coverage_date)
    now = _wall_time(now)
    if now <= start:
        return 0
    if now >= end:
        return required
    curve = _hourly_curve(task.pacing_config or {})
    ratio = _weighted_progress(start, end, now, curve) if curve else _uniform_progress(start, end, now)
    return min(required, max(0, math.floor(required * ratio)))


def active_window_bounds(active_window: str, coverage_date: date) -> tuple[datetime, datetime]:
    start_minutes, end_minutes = _window_minutes(active_window)
    start = datetime.combine(coverage_date, time.min) + timedelta(minutes=start_minutes)
    end_date = coverage_date if end_minutes > start_minutes else coverage_date + timedelta(days=1)
    end = datetime.combine(end_date, time.min) + timedelta(minutes=end_minutes)
    return start, end


def _window_minutes(active_window: str) -> tuple[int, int]:
    try:
        start_raw, end_raw = str(active_window or "09:00-23:00").split("-", 1)
        return _minute_of_day(start_raw), _minute_of_day(end_raw)
    except (TypeError, ValueError):
        raise ValueError(f"invalid group active window: {active_window}")


def _minute_of_day(raw: str) -> int:
    hour, minute = raw.strip().split(":", 1)
    parsed_hour = int(hour)
    parsed_minute = int(minute)
    if not 0 <= parsed_hour <= 23 or not 0 <= parsed_minute <= 59:
        raise ValueError("invalid time")
    return parsed_hour * 60 + parsed_minute


def _hourly_curve(pacing_config: dict) -> list[int]:
    profile = pacing_config.get("operation_profile") or {}
    raw = profile.get("hourly_activity_curve") if isinstance(profile, dict) else None
    if not isinstance(raw, list) or len(raw) != 24:
        return []
    try:
        curve = [max(0, int(value)) for value in raw]
    except (TypeError, ValueError):
        return []
    return curve if sum(curve) > 0 else []


def _weighted_progress(start: datetime, end: datetime, now: datetime, curve: list[int]) -> float:
    total = _weighted_duration(start, end, end, curve)
    if total <= 0:
        return _uniform_progress(start, end, now)
    elapsed = _weighted_duration(start, end, now, curve)
    return min(1.0, max(0.0, elapsed / total))


def _weighted_duration(start: datetime, end: datetime, stop: datetime, curve: list[int]) -> float:
    cursor = start
    total = 0.0
    boundary = min(end, max(start, stop))
    while cursor < boundary:
        next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        segment_end = min(boundary, next_hour)
        total += curve[cursor.hour] * (segment_end - cursor).total_seconds()
        cursor = segment_end
    return total


def _uniform_progress(start: datetime, end: datetime, now: datetime) -> float:
    total = max(1.0, (end - start).total_seconds())
    elapsed = min(total, max(0.0, (now - start).total_seconds()))
    return elapsed / total


def _wall_time(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


__all__ = ["active_window_bounds", "daily_coverage_due_debt"]
