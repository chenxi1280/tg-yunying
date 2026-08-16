from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.timezone import BEIJING_TZ

from .pacing import fulfillment_pacing_config, schedule_due_times, task_pacing_anchor
from .pacing_stratified import pacing_plan_hash


@dataclass(frozen=True)
class SourcePacingSlot:
    source_key: str
    slot_key: str
    slot_ordinal: int
    plan_total: int
    period_start_at: datetime
    deadline_at: datetime
    release_not_before_at: datetime | None = None


@dataclass(frozen=True)
class SourcePacingPoint:
    due_at: datetime
    release_not_before_at: datetime


def rolling_source_window(task, observed_at: datetime) -> tuple[datetime, datetime]:
    source_start = wall_datetime(observed_at)
    task_anchor = task_pacing_anchor(task)
    period_start = max(source_start, task_anchor) if task_anchor else source_start
    return period_start, source_start + timedelta(days=1)


def wall_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)


def latest_wall_datetime(*values: datetime) -> datetime:
    return max(wall_datetime(value) for value in values)


def schedule_source_pacing_slots(
    slots: list[SourcePacingSlot],
    config: dict,
    *,
    seed_id: str,
    now_at: datetime,
    timezone_name: str | None = None,
) -> dict[str, datetime]:
    now_at = wall_datetime(now_at)
    grouped: dict[str, list[SourcePacingSlot]] = defaultdict(list)
    for slot in slots:
        grouped[slot.source_key].append(slot)
    planned: dict[str, datetime] = {}
    for source_key, source_slots in grouped.items():
        first = source_slots[0]
        _validate_source_group(source_slots, first)
        period_start_at = wall_datetime(first.period_start_at)
        deadline_at = wall_datetime(first.deadline_at)
        if deadline_at <= now_at:
            continue
        due_times = schedule_due_times(
            len(source_slots),
            config,
            period_start_at=period_start_at,
            deadline_at=deadline_at,
            timezone_name=timezone_name,
            seed_id=f"{seed_id}:{source_key}",
            slot_keys=[slot.slot_key for slot in source_slots],
            plan_total=first.plan_total,
            slot_ordinals=[slot.slot_ordinal for slot in source_slots],
        )
        planned.update(zip((slot.slot_key for slot in source_slots), due_times, strict=True))
    return planned


def schedule_source_pacing_points(
    slots: list[SourcePacingSlot],
    config: dict,
    *,
    seed_id: str,
    now_at: datetime,
    timezone_name: str | None = None,
) -> dict[str, SourcePacingPoint]:
    due_by_slot = schedule_source_pacing_slots(
        slots,
        config,
        seed_id=seed_id,
        now_at=now_at,
        timezone_name=timezone_name,
    )
    grouped: dict[str, list[SourcePacingSlot]] = defaultdict(list)
    for slot in slots:
        if slot.slot_key in due_by_slot:
            grouped[slot.source_key].append(slot)
    points: dict[str, SourcePacingPoint] = {}
    for source_key, source_slots in grouped.items():
        points.update(_source_recovery_points(
            source_slots,
            due_by_slot,
            now_at=now_at,
            seed_id=f"{seed_id}:{source_key}:recovery",
        ))
    return points


def _source_recovery_points(
    slots: list[SourcePacingSlot],
    due_by_slot: dict[str, datetime],
    *,
    now_at: datetime,
    seed_id: str,
) -> dict[str, SourcePacingPoint]:
    now_at = wall_datetime(now_at)
    result: dict[str, SourcePacingPoint] = {}
    overdue: list[SourcePacingSlot] = []
    frozen_releases: list[datetime] = []
    for slot in slots:
        due_at = wall_datetime(due_by_slot[slot.slot_key])
        frozen = (
            wall_datetime(slot.release_not_before_at)
            if slot.release_not_before_at is not None
            else None
        )
        if due_at >= now_at:
            result[slot.slot_key] = SourcePacingPoint(due_at, due_at)
        elif frozen is not None and frozen > due_at:
            result[slot.slot_key] = SourcePacingPoint(due_at, frozen)
            if frozen > now_at:
                frozen_releases.append(frozen)
        else:
            overdue.append(slot)
    if not overdue:
        return result
    first = slots[0]
    period_start_at = wall_datetime(first.period_start_at)
    deadline_at = wall_datetime(first.deadline_at)
    gap_seconds = max(
        1.0,
        (deadline_at - period_start_at).total_seconds() / first.plan_total,
    )
    cursor = _recovery_cursor(
        frozen_releases, now_at=now_at, gap_seconds=gap_seconds,
    )
    for slot in sorted(overdue, key=lambda item: (
        due_by_slot[item.slot_key], item.slot_ordinal, item.slot_key,
    )):
        cursor += timedelta(seconds=gap_seconds + _recovery_jitter(
            seed_id, slot.slot_key, gap_seconds,
        ))
        if cursor >= deadline_at:
            break
        result[slot.slot_key] = SourcePacingPoint(
            wall_datetime(due_by_slot[slot.slot_key]), cursor,
        )
    return result


def _recovery_cursor(
    frozen_releases: list[datetime],
    *,
    now_at: datetime,
    gap_seconds: float,
) -> datetime:
    if not frozen_releases:
        return now_at - timedelta(seconds=gap_seconds * 0.8)
    return max(frozen_releases)


def _recovery_jitter(seed_id: str, slot_key: str, gap_seconds: float) -> float:
    digest = hashlib.sha256(f"{seed_id}:{slot_key}".encode("utf-8")).digest()
    ratio = int.from_bytes(digest[:2], "big") / 65535
    return gap_seconds * ratio * 0.2


def source_pacing_plan_hash(
    slot: SourcePacingSlot,
    config: dict,
    *,
    seed_id: str,
) -> str:
    effective = fulfillment_pacing_config(config or {})
    profile = effective.get("operation_profile") or {}
    curve = profile.get("hourly_activity_curve") if isinstance(profile, dict) else None
    hourly_curve = list(curve) if isinstance(curve, list) and len(curve) == 24 else []
    return pacing_plan_hash(
        plan_total=slot.plan_total,
        period_start_at=slot.period_start_at,
        deadline_at=slot.deadline_at,
        hourly_curve=hourly_curve,
        seed_id=f"{seed_id}:{slot.source_key}",
    )


def _validate_source_group(slots: list[SourcePacingSlot], expected: SourcePacingSlot) -> None:
    for slot in slots:
        if (
            slot.plan_total != expected.plan_total
            or slot.period_start_at != expected.period_start_at
            or slot.deadline_at != expected.deadline_at
        ):
            raise ValueError("source_pacing_plan_identity_mismatch")


__all__ = [
    "SourcePacingSlot",
    "SourcePacingPoint",
    "latest_wall_datetime",
    "rolling_source_window",
    "schedule_source_pacing_slots",
    "schedule_source_pacing_points",
    "source_pacing_plan_hash",
    "wall_datetime",
]
