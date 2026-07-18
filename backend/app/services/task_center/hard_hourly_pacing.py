from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


HARD_HOURLY_MIN_RECHECK_SECONDS = 30
HARD_HOURLY_SECONDS_PER_HOUR = 60 * 60


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
    planned_recheck_at = _planned_batch_recheck_at(progress, current, created)
    if planned_recheck_at and coverage_recheck_at:
        return max(planned_recheck_at, coverage_recheck_at)
    if planned_recheck_at:
        return planned_recheck_at
    if coverage_recheck_at:
        return coverage_recheck_at
    if blockers.get("ai_generation_unavailable"):
        return current + timedelta(minutes=1)
    if blockers.get("quality_filter"):
        return current + timedelta(seconds=60)
    if blockers.get("dispatcher_lag"):
        return current + timedelta(seconds=30)
    return current + timedelta(seconds=HARD_HOURLY_MIN_RECHECK_SECONDS if int(progress.get("deficit") or 0) else 300)


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
    return min(recheck_at, hour_end) if isinstance(hour_end, datetime) and hour_end > current else recheck_at


def _positive_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
