from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Protocol


SLOT_JITTER_FRACTION = 0.1


class CapacityScopeLike(Protocol):
    source_key_hash: str
    policy_version_id: str
    curve_hash: str
    window_start_at: datetime
    window_end_at: datetime


class CapacityDemandLike(Protocol):
    owner_id: str
    earliest_at: datetime
    latest_at: datetime


def candidate_slots(
    scope: CapacityScopeLike,
    hours: tuple[datetime, ...],
    quotas: tuple[int, ...],
) -> tuple[datetime, ...]:
    slots: list[datetime] = []
    for hour, quota in zip(hours, quotas, strict=True):
        start = max(scope.window_start_at, hour)
        end = min(scope.window_end_at, hour + timedelta(hours=1))
        seconds = max(0.0, (end - start).total_seconds())
        slots.extend(
            start + timedelta(seconds=seconds * _stratified_position(scope, hour, index, quota))
            for index in range(quota)
        )
    return tuple(sorted(slots))


def fit_candidate_slots(
    candidates: tuple[datetime, ...],
    demands: tuple[CapacityDemandLike, ...],
    scope: CapacityScopeLike,
    *,
    occupied: tuple[datetime, ...] = (),
    minimum_gap_seconds: int = 0,
) -> tuple[datetime, ...]:
    remaining = list(enumerate(candidates))
    scheduled: dict[int, datetime] = {}
    blocked = list(occupied)
    ordered = sorted(demands, key=lambda item: (item.latest_at, item.earliest_at, item.owner_id))
    for demand in ordered:
        selection = _select_demand_candidate(
            remaining,
            demand,
            scope,
            blocked,
            minimum_gap_seconds,
        )
        if selection is None:
            continue
        index, scheduled_at = selection
        scheduled[index] = scheduled_at
        blocked.append(scheduled_at)
        remaining = [item for item in remaining if item[0] != index]
    for index, candidate in remaining:
        scheduled_at = _fit_headroom_candidate(
            candidate,
            scope,
            blocked,
            minimum_gap_seconds,
        )
        if scheduled_at is None:
            continue
        scheduled[index] = scheduled_at
        blocked.append(scheduled_at)
    return tuple(sorted(scheduled.values()))


def _stratified_position(
    scope: CapacityScopeLike,
    hour: datetime,
    index: int,
    quota: int,
) -> float:
    ratio = _stable_ratio(scope, f"slot:{hour.isoformat()}:{index}")
    offset = (ratio - 0.5) * SLOT_JITTER_FRACTION
    return (index + 0.5 + offset) / quota


def _stable_ratio(scope: CapacityScopeLike, discriminator: str) -> float:
    payload = ":".join((
        scope.source_key_hash,
        scope.policy_version_id,
        scope.curve_hash,
        discriminator,
    ))
    value = int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:8])
    return (value + 1) / (2**64 + 1)


def _select_demand_candidate(
    candidates: list[tuple[int, datetime]],
    demand: CapacityDemandLike,
    scope: CapacityScopeLike,
    blocked: list[datetime],
    minimum_gap_seconds: int,
) -> tuple[int, datetime] | None:
    valid_raw = [
        (index, candidate)
        for index, candidate in candidates
        if demand.earliest_at <= candidate < demand.latest_at
        and _gap_safe(candidate, blocked, minimum_gap_seconds)
    ]
    if valid_raw:
        return min(valid_raw, key=lambda item: item[1])
    replacements = []
    for index, candidate in candidates:
        interval = _candidate_interval(candidate, demand.earliest_at, demand.latest_at, scope)
        if interval is None:
            continue
        scheduled_at = _earliest_legal_at(*interval, blocked, minimum_gap_seconds)
        if scheduled_at is not None:
            replacements.append((index, scheduled_at))
    return min(replacements, key=lambda item: item[1]) if replacements else None


def _fit_headroom_candidate(
    candidate: datetime,
    scope: CapacityScopeLike,
    blocked: list[datetime],
    minimum_gap_seconds: int,
) -> datetime | None:
    if _gap_safe(candidate, blocked, minimum_gap_seconds):
        return candidate
    interval = _candidate_interval(candidate, scope.window_start_at, scope.window_end_at, scope)
    if interval is None:
        return None
    return _earliest_legal_at(*interval, blocked, minimum_gap_seconds)


def _candidate_interval(
    candidate: datetime,
    earliest_at: datetime,
    latest_at: datetime,
    scope: CapacityScopeLike,
) -> tuple[datetime, datetime] | None:
    hour = candidate.replace(minute=0, second=0, microsecond=0)
    start = max(earliest_at, scope.window_start_at, hour)
    end = min(latest_at, scope.window_end_at, hour + timedelta(hours=1))
    return (start, end) if start < end else None


def _earliest_legal_at(
    start: datetime,
    end: datetime,
    blocked: list[datetime],
    minimum_gap_seconds: int,
) -> datetime | None:
    cursor = start
    gap = timedelta(seconds=max(0, minimum_gap_seconds))
    for item in sorted(blocked):
        if item + gap <= cursor:
            continue
        if cursor + gap <= item:
            break
        cursor = item + gap
    return cursor if cursor < end else None


def _gap_safe(
    candidate: datetime,
    blocked: list[datetime],
    minimum_gap_seconds: int,
) -> bool:
    return all(
        abs((candidate - item).total_seconds()) >= minimum_gap_seconds
        for item in blocked
    )


__all__ = ["candidate_slots", "fit_candidate_slots"]
