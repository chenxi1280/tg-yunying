from __future__ import annotations

import math
import random
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services._common import _now
from app.timezone import BEIJING_TZ

from .pacing_curve_schedule import curve_schedule_times


TEMPLATES = {
    "aggressive_1h": (3600, 30, 90, 40),
    "moderate_6h": (21600, 180, 480, 30),
    "gentle_24h": (86400, 900, 2400, 25),
    "burst_30min": (1800, 15, 45, 50),
}
FULFILLMENT_SOFT_PACING_VERSION = "nonzero_v1"
DEFAULT_AI_ROUNDS_PER_HOUR = 12
MIN_SCHEDULE_GAP_SECONDS = 1


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


def cumulative_pacing_due(
    total_target: int,
    config: dict,
    *,
    anchor_at: datetime,
    period_start_at: datetime,
    period_end_at: datetime,
    now: datetime,
) -> int:
    target = max(0, int(total_target or 0))
    period_start = _wall_time(period_start_at)
    period_end = _wall_time(period_end_at)
    anchor = max(period_start, _wall_time(anchor_at))
    timestamp = min(_wall_time(now), period_end)
    if target <= 0 or period_end <= period_start or timestamp <= anchor:
        return 0
    curve = _positive_hourly_curve(config)
    elapsed_weight = _weighted_seconds(anchor, timestamp, curve)
    period_weight = _weighted_seconds(period_start, period_end, curve)
    due = math.floor(target * elapsed_weight / max(1.0, period_weight))
    return max(1, min(target, due))


def task_pacing_anchor(task) -> datetime | None:
    stats = task.stats if isinstance(task.stats, dict) else {}
    runtime_candidates = [
        _parse_datetime(stats.get("pacing_anchor_at")),
        _parse_datetime(stats.get("started_at")),
    ]
    runtime_values = [
        _wall_time(value)
        for value in runtime_candidates
        if value is not None
    ]
    runtime_start = max(runtime_values) if runtime_values else None
    scheduled_start = _wall_time(task.scheduled_start) if task.scheduled_start else None
    if runtime_start and scheduled_start:
        return max(runtime_start, scheduled_start)
    return runtime_start or scheduled_start or _wall_time(task.created_at)


def source_rolling_pacing_due(
    total_target: int,
    config: dict,
    *,
    task,
    source_observed_at: datetime,
    now: datetime,
) -> int:
    source_anchor = _wall_time(source_observed_at)
    task_anchor = task_pacing_anchor(task)
    anchor = max(source_anchor, task_anchor) if task_anchor else source_anchor
    return cumulative_pacing_due(
        total_target,
        config,
        anchor_at=anchor,
        period_start_at=anchor,
        period_end_at=anchor + timedelta(days=1),
        now=now,
    )


def _parse_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("task_pacing_anchor_invalid") from exc


def _wall_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)


def _positive_hourly_curve(config: dict) -> list[int]:
    curve = _operation_curve(_effective_fulfillment_config(config))
    return [max(1, value) for value in curve] if curve else [1] * 24


def _weighted_seconds(start: datetime, end: datetime, curve: list[int]) -> float:
    cursor = start
    total = 0.0
    while cursor < end:
        next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        boundary = min(end, next_hour)
        total += curve[cursor.hour] * (boundary - cursor).total_seconds()
        cursor = boundary
    return total


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
    max_per_hour = int(config.get("max_actions_per_hour") or 0)
    return curve_schedule_times(total_actions, curve, max_per_hour, start_at)


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


def minimum_schedule_gap_seconds(config: dict) -> int:
    effective = _effective_fulfillment_config(config or {})
    _duration, interval_min, _interval_max, _jitter = _duration_and_interval(effective, 1)
    if (effective.get("mode") or "template") == "fixed" and fixed_interval_is_immediate(effective):
        interval_min = 0
    hourly_cap = int(effective.get("max_actions_per_hour") or 0)
    hourly_gap = math.ceil(3600 / hourly_cap) if hourly_cap > 0 else 0
    return max(MIN_SCHEDULE_GAP_SECONDS, interval_min, hourly_gap)


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


def fulfillment_pacing_config(config: dict) -> dict:
    normalized = dict(config or {})
    quiet = normalized.pop("quiet_hours", None)
    curve = _operation_curve(normalized)
    if not curve:
        return normalized
    profile = dict(normalized.get("operation_profile") or {})
    curve = [max(1, value) for value in curve]
    if quiet:
        start, end = _quiet_hours_window(quiet)
        quiet_weight = max(1, int(profile.get("quiet_threshold") or 2))
        curve = [
            min(value, quiet_weight) if _hour_is_quiet(hour, start, end) else value
            for hour, value in enumerate(curve)
        ]
    profile["hourly_activity_curve"] = curve
    normalized["operation_profile"] = profile
    return normalized


def _hour_is_quiet(hour: int, start: time, end: time) -> bool:
    current = time(hour=hour)
    return start <= current < end if start < end else current >= start or current < end


def _effective_fulfillment_config(config: dict) -> dict:
    if config.get("fulfillment_soft_pacing_version") == FULFILLMENT_SOFT_PACING_VERSION:
        return fulfillment_pacing_config(config)
    return config


def schedule_times(
    total_actions: int,
    config: dict,
    *,
    start_at: datetime | None = None,
    deadline_at: datetime | None = None,
    preserve_minimum_spacing: bool = False,
) -> list[datetime]:
    if total_actions <= 0:
        return []
    config = _effective_fulfillment_config(config)
    now = start_at or _now()
    times = _initial_schedule_times(total_actions, config, now)
    return _finalize_schedule(
        times,
        config,
        now,
        deadline_at,
        preserve_minimum_spacing=preserve_minimum_spacing,
    )


def schedule_due_times(
    total_actions: int,
    config: dict,
    *,
    start_at: datetime | None = None,
    deadline_at: datetime | None = None,
    timezone_name: str | None = None,
    deadline_is_utc: bool = False,
) -> list[datetime]:
    if total_actions <= 0:
        return []
    earliest = _next_active_time(
        start_at or _now(),
        config or {},
        timezone_name=timezone_name,
    )
    deadline = _schedule_deadline(
        deadline_at,
        earliest,
        deadline_is_utc=deadline_is_utc,
    )
    if not _before_half_open_deadline(earliest, deadline):
        return []
    return [earliest for _ in range(total_actions)]


def _schedule_deadline(
    deadline_at: datetime | None,
    reference: datetime,
    *,
    deadline_is_utc: bool,
) -> datetime | None:
    if deadline_at is None or not deadline_is_utc:
        return deadline_at
    source = deadline_at if deadline_at.tzinfo else deadline_at.replace(tzinfo=timezone.utc)
    if reference.tzinfo is None:
        return source.astimezone(BEIJING_TZ).replace(tzinfo=None)
    return source.astimezone(reference.tzinfo)


def _before_half_open_deadline(
    value: datetime,
    deadline_at: datetime | None,
) -> bool:
    if deadline_at is None:
        return True
    deadline = deadline_at
    if value.tzinfo is None and deadline.tzinfo is not None:
        deadline = deadline.replace(tzinfo=None)
    elif value.tzinfo is not None and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=value.tzinfo)
    elif value.tzinfo is not None and deadline.tzinfo is not None:
        deadline = deadline.astimezone(value.tzinfo)
    return value < deadline


def _initial_schedule_times(total_actions: int, config: dict, now: datetime) -> list[datetime]:
    mode = config.get("mode") or "template"
    if mode == "fixed" and fixed_interval_is_immediate(config):
        return [now for _ in range(total_actions)]
    curve_times = [] if mode == "fixed" else _curve_schedule_times(total_actions, config, now)
    if curve_times:
        return curve_times
    return _interval_schedule_times(total_actions, config, now, mode)


def _interval_schedule_times(
    total_actions: int,
    config: dict,
    now: datetime,
    mode: str,
) -> list[datetime]:
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
    return times


def _finalize_schedule(
    times: list[datetime],
    config: dict,
    start_at: datetime,
    deadline_at: datetime | None,
    *,
    preserve_minimum_spacing: bool,
) -> list[datetime]:
    max_per_hour = config.get("max_actions_per_hour")
    if max_per_hour:
        min_gap = int(3600 / max(1, int(max_per_hour)))
        times = _enforce_minimum_spacing(times, min_gap)
    if preserve_minimum_spacing:
        times = _enforce_minimum_spacing(times, minimum_schedule_gap_seconds(config))
    adjusted = _apply_quiet_hours_preserving_spacing(times, config)
    if preserve_minimum_spacing:
        return _truncate_after_deadline(adjusted, deadline_at)
    return _fit_before_deadline(adjusted, start_at, deadline_at)


def _enforce_minimum_spacing(times: list[datetime], minimum_gap_seconds: int) -> list[datetime]:
    adjusted: list[datetime] = []
    gap = timedelta(seconds=max(MIN_SCHEDULE_GAP_SECONDS, minimum_gap_seconds))
    for value in sorted(times):
        adjusted.append(max(value, adjusted[-1] + gap) if adjusted else value)
    return adjusted


def _truncate_after_deadline(times: list[datetime], deadline_at: datetime | None) -> list[datetime]:
    if deadline_at is None:
        return times
    return [value for value in times if value <= deadline_at]


def _apply_quiet_hours_preserving_spacing(times: list[datetime], config: dict) -> list[datetime]:
    ordered = sorted(times)
    adjusted: list[datetime] = []
    for index, original in enumerate(ordered):
        candidate = _apply_quiet_hours(original, config)
        if index:
            original_gap = original - ordered[index - 1]
            candidate = max(candidate, adjusted[-1] + original_gap)
            candidate = _apply_quiet_hours(candidate, config)
        adjusted.append(candidate)
    return adjusted


def _fit_before_deadline(
    times: list[datetime],
    start_at: datetime,
    deadline_at: datetime | None,
) -> list[datetime]:
    if not times or deadline_at is None or max(times) <= deadline_at:
        return times
    if deadline_at <= start_at:
        return []
    if len(times) == 1:
        return [start_at]
    count = len(times)
    duration = (deadline_at - start_at).total_seconds()
    return [
        start_at + timedelta(seconds=duration * index / count)
        for index in range(count)
    ]


def next_local_day_deadline(
    value: datetime,
    timezone_name: str,
) -> datetime:
    local_value = _task_local_datetime(value, timezone_name)
    local_deadline = (
        local_value.replace(hour=0, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
        - timedelta(microseconds=1)
    )
    return _from_task_local_datetime(local_deadline, value, timezone_name)


def fixed_interval_is_immediate(config: dict) -> bool:
    if config.get("interval_seconds_min") is None and config.get("interval_seconds_max") is None:
        return False
    return int(config.get("interval_seconds_min") or 0) <= 0 and int(config.get("interval_seconds_max") or 0) <= 0


def next_run_after(config: dict, *, timezone_name: str | None = None) -> datetime:
    config = _effective_fulfillment_config(config)
    if (config.get("mode") or "template") == "fixed":
        raw_interval = config.get("interval_seconds_min")
        if raw_interval is None:
            raw_interval = config.get("interval_seconds_max")
        interval = int(300 if raw_interval is None else raw_interval)
        return _next_active_time(_now() + timedelta(seconds=max(0, interval)), config, timezone_name=timezone_name)
    return _next_active_time(_now() + timedelta(minutes=5), config, timezone_name=timezone_name)


def ai_next_run_after(config: dict, value: datetime | None = None) -> datetime:
    current = value or _now()
    effective = fulfillment_pacing_config(config)
    rounds = (
        current_hour_rounds(effective, current)
        or DEFAULT_AI_ROUNDS_PER_HOUR
    )
    interval_seconds = max(60, 3600 // max(1, rounds))
    return _next_active_time(
        current + timedelta(seconds=interval_seconds),
        effective,
    )


__all__ = [
    "ai_next_run_after",
    "cumulative_pacing_due",
    "current_hour_rounds",
    "FULFILLMENT_SOFT_PACING_VERSION",
    "fulfillment_pacing_config",
    "next_local_day_deadline",
    "next_run_after",
    "operation_intensity",
    "quiet_hours_active",
    "schedule_due_times",
    "schedule_times",
    "source_rolling_pacing_due",
    "task_pacing_anchor",
]
