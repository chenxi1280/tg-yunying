from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .datetime_compat import ensure_aware, to_zone


HARD_HOURLY_MIN_RECHECK_SECONDS = 30
HARD_HOURLY_SECONDS_PER_HOUR = 60 * 60
DAILY_COVERAGE_RECHECK_SECONDS = 120
DAILY_COVERAGE_RECHECK_BLOCKERS = frozenset({"coverage_waiting", "daily_coverage_capacity_insufficient"})


def planning_rate(progress: dict[str, Any]) -> int:
    hourly_goal = max(1, _positive_int(progress.get("goal")))
    backfill_deficit = _positive_int(progress.get("backfill_planning_deficit"))
    return hourly_goal + min(hourly_goal, backfill_deficit)


def next_check_at(
    blockers: dict[str, int],
    progress: dict[str, Any],
    current: datetime,
    *,
    created: int = 0,
    coverage_recheck_at: datetime | None = None,
) -> datetime:
    current = ensure_aware(current)
    planned_recheck_at = _planned_batch_recheck_at(progress, current, created)
    coverage_checkpoint = (
        to_zone(coverage_recheck_at, current.tzinfo)
        if coverage_recheck_at is not None
        else None
    )
    if planned_recheck_at and coverage_checkpoint:
        return max(planned_recheck_at, coverage_checkpoint)
    if planned_recheck_at:
        return planned_recheck_at
    if coverage_checkpoint:
        return coverage_checkpoint
    if blockers.get("ai_generation_unavailable"):
        return current + timedelta(minutes=1)
    if blockers.get("quality_filter"):
        return current + timedelta(seconds=60)
    if blockers.get("dispatcher_lag"):
        return current + timedelta(seconds=30)
    return current + timedelta(seconds=HARD_HOURLY_MIN_RECHECK_SECONDS if int(progress.get("deficit") or 0) else 300)


def daily_coverage_recheck_at(
    blockers: dict[str, int], current: datetime, checkpoint: datetime | None,
) -> datetime | None:
    if not DAILY_COVERAGE_RECHECK_BLOCKERS.intersection(blockers):
        return None
    current = ensure_aware(current)
    if checkpoint is not None:
        return max(current, to_zone(checkpoint, current.tzinfo))
    return current + timedelta(seconds=DAILY_COVERAGE_RECHECK_SECONDS)


def _planned_batch_recheck_at(progress: dict[str, Any], current: datetime, created: int) -> datetime | None:
    created_count = max(0, int(created or 0))
    if not created_count:
        return None
    rate = planning_rate(progress)
    interval_seconds = max(
        HARD_HOURLY_MIN_RECHECK_SECONDS,
        (HARD_HOURLY_SECONDS_PER_HOUR * created_count + rate - 1) // rate,
    )
    recheck_at = current + timedelta(seconds=interval_seconds)
    hour_end = progress.get("hour_end")
    if not isinstance(hour_end, datetime):
        return recheck_at
    normalized_hour_end = to_zone(hour_end, current.tzinfo)
    return min(recheck_at, normalized_hour_end) if normalized_hour_end > current else recheck_at


def _positive_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
