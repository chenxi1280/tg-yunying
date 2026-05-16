from __future__ import annotations

import random
from datetime import datetime, time, timedelta

from app.services._common import _now


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
            curve.append(min(100, max(0, int(item))))
        except (TypeError, ValueError):
            curve.append(0)
    return curve


def operation_intensity(config: dict, value: datetime | None = None) -> tuple[str, float, int]:
    current = value or _now()
    curve = _operation_curve(config)
    if not curve:
        return "正常期", 1.0, 100
    profile = config.get("operation_profile") or {}
    quiet_threshold = int(profile.get("quiet_threshold") or 20)
    peak_threshold = int(profile.get("peak_threshold") or 70)
    intensity = int(curve[current.hour])
    if intensity <= 0:
        return "休眠期", 0.0, intensity
    if intensity <= quiet_threshold:
        return "低频期", max(0.05, intensity / 100), intensity
    if intensity >= peak_threshold:
        return "高峰期", min(1.0, intensity / 100), intensity
    return "正常期", min(1.0, intensity / 100), intensity


def _next_active_time(value: datetime, config: dict) -> datetime:
    curve = _operation_curve(config)
    if not curve:
        return _apply_quiet_hours(value, config)
    if curve[value.hour] > 0:
        return value
    cursor = value.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(24):
        if curve[cursor.hour] > 0:
            return cursor
        cursor += timedelta(hours=1)
    return value


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


def _apply_quiet_hours(value: datetime, config: dict) -> datetime:
    quiet = config.get("quiet_hours") or None
    if not quiet:
        return value
    start_raw = str(quiet.get("start") or "02:00")
    end_raw = str(quiet.get("end") or "08:00")
    try:
        start_hour, start_minute = [int(item) for item in start_raw.split(":", 1)]
        end_hour, end_minute = [int(item) for item in end_raw.split(":", 1)]
    except ValueError:
        return value
    start = time(start_hour, start_minute)
    end = time(end_hour, end_minute)
    current = value.time()
    in_quiet = start <= current < end if start < end else current >= start or current < end
    if not in_quiet:
        return value
    next_end = value.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if start >= end and current >= start:
        next_end += timedelta(days=1)
    return next_end


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


def next_run_after(config: dict) -> datetime:
    if (config.get("mode") or "template") == "fixed":
        raw_interval = config.get("interval_seconds_min")
        if raw_interval is None:
            raw_interval = config.get("interval_seconds_max")
        interval = int(300 if raw_interval is None else raw_interval)
        return _next_active_time(_now() + timedelta(seconds=max(0, interval)), config)
    return _next_active_time(_now() + timedelta(minutes=5), config)


__all__ = ["next_run_after", "operation_intensity", "schedule_times"]
