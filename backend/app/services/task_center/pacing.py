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
    duration, lo, hi, jitter = _duration_and_interval(config, total_actions)
    mode = config.get("mode") or "template"
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


def next_run_after(config: dict) -> datetime:
    if (config.get("mode") or "template") == "fixed":
        raw_interval = config.get("interval_seconds_min")
        if raw_interval is None:
            raw_interval = config.get("interval_seconds_max")
        interval = int(300 if raw_interval is None else raw_interval)
        return _apply_quiet_hours(_now() + timedelta(seconds=max(0, interval)), config)
    return _apply_quiet_hours(_now() + timedelta(minutes=5), config)


__all__ = ["next_run_after", "schedule_times"]
