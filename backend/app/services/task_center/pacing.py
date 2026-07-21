from __future__ import annotations

import random
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.services._common import _now
from app.timezone import BEIJING_TZ


TEMPLATES = {
    "aggressive_1h": (3600, 30, 90, 40),
    "moderate_6h": (21600, 180, 480, 30),
    "gentle_24h": (86400, 900, 2400, 25),
    "burst_30min": (1800, 15, 45, 50),
}


def _operation_curve(config: dict) -> list[int]:
    profile = config.get("operation_profile") or {}
    raw_curve = profile.get("hourly_activity_curve") if isinstance(profile, dict) else None
    if not isinstance(raw_curve, list) or len(raw_curve) != 24:
        return []
    curve: list[int] = []
    for item in raw_curve:
        try:
            curve.append(min(60, max(0, int(item))))
        except (TypeError, ValueError):
            curve.append(0)
    return curve


def current_hour_rounds(config: dict, value: datetime | None = None) -> int:
    current = value or _now()
    curve = _operation_curve(config)
    if not curve:
        return 0
    return max(0, int(curve[current.hour]))


def operation_intensity(config: dict, value: datetime | None = None) -> tuple[str, float, int]:
    current = value or _now()
    curve = _operation_curve(config)
    if not curve:
        return "正常期", 1.0, 100
    profile = config.get("operation_profile") or {}
    quiet_threshold = int(profile.get("quiet_threshold") or 2)
    peak_threshold = int(profile.get("peak_threshold") or 8)
    intensity = int(curve[current.hour])
    if intensity <= 0:
        return "休眠期", 0.0, intensity
    if intensity <= quiet_threshold:
        return "低频期", max(0.05, intensity / 100), intensity
    if intensity >= peak_threshold:
        return "高峰期", min(1.0, intensity / 100), intensity
    return "正常期", min(1.0, intensity / 100), intensity


def _next_active_time(value: datetime, config: dict, *, timezone_name: str | None = None) -> datetime:
    curve = _operation_curve(config)
    if curve and not any(curve):
        return value
    candidate = value
    for _ in range(25):
        local_candidate = _task_local_datetime(candidate, timezone_name)
        if curve and curve[local_candidate.hour] <= 0:
            next_hour = local_candidate.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            candidate = _from_task_local_datetime(next_hour, value, timezone_name)
            continue
        active_candidate = _apply_quiet_hours(candidate, config, timezone_name=timezone_name)
        if active_candidate == candidate:
            return candidate
        candidate = active_candidate
    return candidate


def _curve_schedule_times(total_actions: int, config: dict, start_at: datetime) -> list[datetime]:
    curve = _operation_curve(config)
    total_weight = sum(curve)
    if total_weight <= 0:
        return []
    max_per_hour = int(config.get("max_actions_per_hour") or 0)
    slots: list[tuple[int, int, float]] = []
    for offset in range(24):
        hour = (start_at.hour + offset) % 24
        weight = curve[hour]
        if weight <= 0:
            continue
        exact = total_actions * (weight / total_weight)
        count = int(exact)
        if max_per_hour:
            count = min(count, max_per_hour)
        slots.append((offset, count, exact - count))
    assigned = sum(count for _offset, count, _fraction in slots)
    capacity = sum((max_per_hour or total_actions) - count for _offset, count, _fraction in slots)
    remaining = min(total_actions - assigned, max(0, capacity))
    slots = sorted(slots, key=lambda item: item[2], reverse=True)
    next_slots: list[tuple[int, int, float]] = []
    for offset, count, fraction in slots:
        if remaining > 0 and (not max_per_hour or count < max_per_hour):
            count += 1
            remaining -= 1
        next_slots.append((offset, count, fraction))
    result: list[datetime] = []
    for offset, count, _fraction in sorted(next_slots, key=lambda item: item[0]):
        if count <= 0:
            continue
        hour_start = start_at.replace(minute=0, second=0, microsecond=0) + timedelta(hours=offset)
        if offset == 0:
            hour_start = max(start_at, hour_start)
        available_seconds = max(1, int(((hour_start.replace(minute=59, second=59, microsecond=0) - hour_start).total_seconds())))
        step = max(1, available_seconds // max(count, 1))
        for index in range(count):
            result.append(hour_start + timedelta(seconds=min(available_seconds, index * step)))
    return sorted(result)[:total_actions]


def _duration_and_interval(config: dict, total: int) -> tuple[int, int, int, int]:
    mode = config.get("mode") or "template"
    if mode == "fixed":
        lo = int(config.get("interval_seconds_min") or 60)
        hi = max(lo, int(config.get("interval_seconds_max") or lo))
        return max(hi * max(total, 1), hi), lo, hi, int(config.get("jitter_percent") or 0)
    if mode == "curve":
        duration = int(config.get("curve_duration_hours") or 6) * 3600
        interval = max(1, duration // max(total, 1))
        return duration, max(1, int(interval * 0.7)), max(1, int(interval * 1.3)), int(config.get("jitter_percent") or 30)
    duration, lo, hi, jitter = TEMPLATES.get(config.get("template") or "moderate_6h", TEMPLATES["moderate_6h"])
    return duration, lo, hi, int(config.get("jitter_percent") or jitter)


def quiet_hours_active(value: datetime, config: dict, *, timezone_name: str | None = None) -> bool:
    quiet = config.get("quiet_hours") or None
    if not quiet:
        return False
    start, end = _quiet_hours_window(quiet)
    current = _quiet_hours_local_time(value, timezone_name)
    return start <= current < end if start < end else current >= start or current < end


def _quiet_hours_local_time(value: datetime, timezone_name: str | None) -> time:
    return _task_local_datetime(value, timezone_name).time()


def _task_local_datetime(value: datetime, timezone_name: str | None) -> datetime:
    if not timezone_name:
        return value
    source = value if value.tzinfo else value.replace(tzinfo=BEIJING_TZ)
    return source.astimezone(ZoneInfo(timezone_name))


def _from_task_local_datetime(value: datetime, original: datetime, timezone_name: str | None) -> datetime:
    if not timezone_name:
        return value
    beijing_value = value.astimezone(BEIJING_TZ)
    return beijing_value.replace(tzinfo=None) if original.tzinfo is None else beijing_value.astimezone(original.tzinfo)


def _apply_quiet_hours(value: datetime, config: dict, *, timezone_name: str | None = None) -> datetime:
    quiet = config.get("quiet_hours") or None
    if not quiet:
        return value
    start, end = _quiet_hours_window(quiet)
    if not quiet_hours_active(value, config, timezone_name=timezone_name):
        return value
    if timezone_name:
        return _quiet_hours_end_in_task_timezone(value, start, end, timezone_name)
    next_end = value.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if start >= end and value.time() >= start:
        next_end += timedelta(days=1)
    return next_end


def _quiet_hours_end_in_task_timezone(value: datetime, start: time, end: time, timezone_name: str) -> datetime:
    local_value = _task_local_datetime(value, timezone_name)
    next_end = local_value.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if start >= end and local_value.time() >= start:
        next_end += timedelta(days=1)
    return _from_task_local_datetime(next_end, value, timezone_name)


def _quiet_hours_window(quiet: dict) -> tuple[time, time]:
    start_raw = str(quiet.get("start") or "")
    end_raw = str(quiet.get("end") or "")
    try:
        start = datetime.strptime(start_raw, "%H:%M").time()
        end = datetime.strptime(end_raw, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("quiet_hours 必须使用 HH:MM 格式") from exc
    if start == end:
        raise ValueError("quiet_hours.start 与 quiet_hours.end 不能相同")
    return start, end


def schedule_times(total_actions: int, config: dict, *, start_at: datetime | None = None) -> list[datetime]:
    if total_actions <= 0:
        return []
    now = start_at or _now()
    mode = config.get("mode") or "template"
    if mode == "fixed" and _fixed_interval_is_immediate(config):
        return [now for _ in range(total_actions)]
    curve_times = [] if mode == "fixed" else _curve_schedule_times(total_actions, config, now)
    if curve_times:
        return curve_times
    duration, lo, hi, jitter = _duration_and_interval(config, total_actions)
    times: list[datetime] = []
    if mode == "curve":
        curve_type = config.get("curve_type") or "steady"
        for index in range(total_actions):
            ratio = index / max(total_actions - 1, 1)
            if curve_type == "front_heavy":
                ratio = ratio**1.7
            elif curve_type == "back_heavy":
                ratio = 1 - ((1 - ratio) ** 1.7)
            elif curve_type == "random_burst":
                ratio = min(1, max(0, ratio + random.uniform(-0.08, 0.08)))
            seconds = int(duration * ratio)
            spread = int(seconds * jitter / 100)
            times.append(now + timedelta(seconds=max(0, seconds + random.randint(-spread, spread) if spread else seconds)))
    else:
        cursor = now
        for _ in range(total_actions):
            interval = random.randint(lo, hi)
            spread = int(interval * jitter / 100)
            if spread:
                interval = max(0, interval + random.randint(-spread, spread))
            times.append(cursor)
            cursor += timedelta(seconds=interval)
    max_per_hour = config.get("max_actions_per_hour")
    if max_per_hour:
        min_gap = int(3600 / max(1, int(max_per_hour)))
        for index in range(1, len(times)):
            floor = times[index - 1] + timedelta(seconds=min_gap)
            if times[index] < floor:
                times[index] = floor
    return [_apply_quiet_hours(item, config) for item in sorted(times)]


def _fixed_interval_is_immediate(config: dict) -> bool:
    if config.get("interval_seconds_min") is None and config.get("interval_seconds_max") is None:
        return False
    return int(config.get("interval_seconds_min") or 0) <= 0 and int(config.get("interval_seconds_max") or 0) <= 0


def next_run_after(config: dict, *, timezone_name: str | None = None) -> datetime:
    if (config.get("mode") or "template") == "fixed":
        raw_interval = config.get("interval_seconds_min")
        if raw_interval is None:
            raw_interval = config.get("interval_seconds_max")
        interval = int(300 if raw_interval is None else raw_interval)
        return _next_active_time(_now() + timedelta(seconds=max(0, interval)), config, timezone_name=timezone_name)
    return _next_active_time(_now() + timedelta(minutes=5), config, timezone_name=timezone_name)


def ai_next_run_after(config: dict, value: datetime | None = None) -> datetime:
    current = value or _now()
    rounds = current_hour_rounds(config, current)
    if rounds <= 0:
        return _next_active_time(current, config)
    interval_seconds = max(60, 3600 // max(1, rounds))
    return _next_active_time(current + timedelta(seconds=interval_seconds), config)


__all__ = ["ai_next_run_after", "current_hour_rounds", "next_run_after", "operation_intensity", "quiet_hours_active", "schedule_times"]
