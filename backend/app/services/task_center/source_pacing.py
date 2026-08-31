from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.timezone import BEIJING_TZ

from .pacing import fulfillment_pacing_config, schedule_due_times, task_pacing_anchor
from .pacing_persistence import PacingOwnerImmutableConflict
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
    frozen_due_at: datetime | None = None
    owner_id: str = ""
    task_lifecycle_epoch: int = 1
    pacing_period_key: str = ""
    pacing_source_key_hash: str = ""
    historical_cursor_at: datetime | None = None
    historical_max_ordinal: int | None = None
    source_capacity_plan_hash: str | None = None
    source_capacity_slot_ordinal: int | None = None

    @property
    def owner_identity(self) -> tuple[int, str, str]:
        return (
            self.task_lifecycle_epoch,
            self.pacing_period_key,
            self.pacing_source_key_hash,
        )


@dataclass(frozen=True)
class SourcePacingPoint:
    due_at: datetime
    release_not_before_at: datetime


def source_window_days(task) -> int:
    pacing_config = getattr(task, "pacing_config", None) or {}
    type_config = getattr(task, "type_config", None) or {}
    days = pacing_config.get("rolling_window_days") or type_config.get("rolling_window_days")
    if days:
        try:
            return max(1, int(days))
        except (TypeError, ValueError):
            pass
    return 3 if getattr(task, "type", "") == "channel_comment" else 1


def rolling_source_window(task, observed_at: datetime) -> tuple[datetime, datetime]:
    source_start = wall_datetime(observed_at)
    task_anchor = task_pacing_anchor(task)
    period_start = max(source_start, task_anchor) if task_anchor else source_start
    days = source_window_days(task)
    return period_start, source_start + timedelta(days=days)


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
        frozen_slots = [slot for slot in source_slots if slot.frozen_due_at is not None]
        planned.update({
            slot.slot_key: wall_datetime(slot.frozen_due_at)
            for slot in frozen_slots
        })
        fresh_slots = [slot for slot in source_slots if slot.frozen_due_at is None]
        if not fresh_slots:
            continue
        first = fresh_slots[0]
        _validate_source_group(fresh_slots, first)
        period_start_at = wall_datetime(first.period_start_at)
        deadline_at = wall_datetime(first.deadline_at)
        if deadline_at <= now_at:
            continue
        due_times = schedule_due_times(
            len(fresh_slots),
            config,
            period_start_at=period_start_at,
            deadline_at=deadline_at,
            timezone_name=timezone_name,
            seed_id=f"{seed_id}:{source_key}",
            slot_keys=[slot.slot_key for slot in fresh_slots],
            plan_total=first.plan_total,
            slot_ordinals=[slot.slot_ordinal for slot in fresh_slots],
        )
        planned.update(zip((slot.slot_key for slot in fresh_slots), due_times, strict=True))
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
    history = [
        wall_datetime(slot.historical_cursor_at)
        for slot in slots
        if slot.historical_cursor_at is not None
    ]
    if history:
        return _points_after_historical_cursor(
            slots,
            due_by_slot,
            cursor=max(history),
            now_at=now_at,
            seed_id=seed_id,
        )
    return _legacy_source_recovery_points(
        slots,
        due_by_slot,
        now_at=now_at,
        seed_id=seed_id,
    )


def _legacy_source_recovery_points(
    slots: list[SourcePacingSlot],
    due_by_slot: dict[str, datetime],
    *,
    now_at: datetime,
    seed_id: str,
) -> dict[str, SourcePacingPoint]:
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
        elif frozen is not None and frozen > due_at and frozen > now_at:
            result[slot.slot_key] = SourcePacingPoint(due_at, frozen)
            frozen_releases.append(frozen)
        else:
            overdue.append(slot)
    if not overdue:
        return result
    first = max(slots, key=lambda slot: slot.plan_total)
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


def _points_after_historical_cursor(
    slots: list[SourcePacingSlot],
    due_by_slot: dict[str, datetime],
    *,
    cursor: datetime,
    now_at: datetime,
    seed_id: str,
) -> dict[str, SourcePacingPoint]:
    first = max(slots, key=lambda slot: slot.plan_total)
    gap_seconds = max(
        1.0,
        (wall_datetime(first.deadline_at) - wall_datetime(first.period_start_at)).total_seconds()
        / first.plan_total,
    )
    cursor = max(cursor, now_at - timedelta(seconds=gap_seconds * 0.8))
    deadline = wall_datetime(first.deadline_at)
    result: dict[str, SourcePacingPoint] = {}
    ordered = sorted(slots, key=lambda item: (
        due_by_slot[item.slot_key], item.slot_ordinal, item.slot_key,
    ))
    for slot in ordered:
        due_at = wall_datetime(due_by_slot[slot.slot_key])
        frozen = wall_datetime(slot.release_not_before_at) if slot.release_not_before_at else None
        if (
            frozen is not None
            and frozen > now_at
            and frozen >= cursor
            and frozen > due_at
        ):
            release_at = frozen
        elif due_at >= now_at and due_at >= cursor + timedelta(seconds=gap_seconds):
            release_at = due_at
        else:
            release_at = cursor + timedelta(
                seconds=gap_seconds + _recovery_jitter(seed_id, slot.slot_key, gap_seconds)
            )
        if release_at >= deadline:
            break
        result[slot.slot_key] = SourcePacingPoint(due_at, release_at)
        cursor = max(cursor, release_at)
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
            raise PacingOwnerImmutableConflict("source_pacing_plan_identity_mismatch")
        if (
            slot.release_not_before_at is None
            and slot.historical_max_ordinal is not None
            and slot.slot_ordinal <= slot.historical_max_ordinal
        ):
            raise PacingOwnerImmutableConflict("pacing_source_cursor_conflict")


__all__ = [
    "SourcePacingSlot",
    "SourcePacingPoint",
    "latest_wall_datetime",
    "rolling_source_window",
    "schedule_source_pacing_slots",
    "schedule_source_pacing_points",
    "source_pacing_plan_hash",
    "source_window_days",
    "wall_datetime",
]
