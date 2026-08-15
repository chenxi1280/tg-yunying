from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.timezone import BEIJING_TZ

from .pacing import _effective_fulfillment_config, _operation_curve


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
    runtime_values = [_wall_time(value) for value in runtime_candidates if value is not None]
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


__all__ = ["cumulative_pacing_due", "source_rolling_pacing_due", "task_pacing_anchor"]
