from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    SourcePacingCapacityPlan,
    SourcePacingCapacityPolicyVersion,
    Task,
)

from .source_capacity_slots import candidate_slots, fit_candidate_slots
from .source_capacity_repository import (
    latest_capacity_component_plans,
    lock_source_capacity,
    overlapping_scope_window,
    source_capacity_plan,
)
from .source_pacing import SourcePacingPoint, SourcePacingSlot, wall_datetime


HOURS_PER_DAY = 24
SOURCE_CAPACITY_FLAG = "source_capacity_v2_enabled"


class SourceCapacityConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class CapacityPolicy:
    hourly_curve: tuple[float, ...]
    minimum_gap_seconds: int
    hourly_ceiling: int
    headroom_floor: float
    provider_retry_slots: int


@dataclass(frozen=True)
class CapacityDemand:
    owner_id: str
    earliest_at: datetime
    latest_at: datetime


@dataclass(frozen=True)
class CapacityScope:
    tenant_id: int
    pacing_domain: str
    source_key_hash: str
    window_start_at: datetime
    window_end_at: datetime
    policy_version_id: str
    curve_hash: str


@dataclass(frozen=True)
class CapacityBaseline:
    capacity_slots: tuple[datetime, ...]
    incoming_count: int
    replacement_headroom: int


@dataclass(frozen=True)
class CapacityResult:
    capacity_slots: tuple[datetime, ...]
    owner_slot_ordinals: dict[str, int]
    occupied_count: int
    incoming_count: int
    replacement_headroom: int
    available_count: int
    deficit_count: int
    last_safe_release_at: datetime | None

    @property
    def admitted(self) -> bool:
        return self.deficit_count == 0


def apply_source_capacity_plan(
    session: Session,
    task: Task,
    slots: list[SourcePacingSlot],
    *,
    points: dict[str, SourcePacingPoint],
    pacing_domain: str,
) -> tuple[dict[str, SourcePacingPoint], list[SourcePacingSlot]]:
    if not bool(dict(task.pacing_config or {}).get(SOURCE_CAPACITY_FLAG)):
        return points, slots
    policy_id = str(
        dict(task.pacing_config or {}).get("source_capacity_policy_version_id") or ""
    )
    policy_row = _active_policy(session, task.tenant_id, pacing_domain, policy_id)
    grouped = _group_slots(slots, points)
    adjusted = dict(points)
    enriched: dict[str, SourcePacingSlot] = {}
    for source_hash, source_slots in grouped.items():
        if all(slot.source_capacity_plan_hash for slot in source_slots):
            enriched.update((slot.slot_key, slot) for slot in source_slots)
            continue
        initial_scope = _capacity_scope(
            policy_row,
            pacing_domain,
            source_hash,
            slots=source_slots,
        )
        lock_source_capacity(session, initial_scope)
        window_start, window_end = overlapping_scope_window(session, initial_scope)
        scope = replace(initial_scope, window_start_at=window_start, window_end_at=window_end)
        baseline = _source_capacity_baseline(session, scope)
        result = plan_source_capacity(
            scope,
            _capacity_policy(policy_row),
            occupied_at=_occupied_releases(source_slots),
            demands=_capacity_demands(source_slots, points),
            baseline=baseline,
        )
        if not result.admitted:
            raise SourceCapacityConflict("source_capacity_insufficient")
        plan = freeze_source_capacity_plan(session, scope, result)
        _apply_capacity_assignments(
            source_slots,
            points,
            result,
            adjusted=adjusted,
            enriched=enriched,
            plan_hash=plan.plan_hash,
        )
    return adjusted, [enriched.get(slot.slot_key, slot) for slot in slots]


def _active_policy(
    session: Session,
    tenant_id: int,
    pacing_domain: str,
    policy_id: str,
) -> SourcePacingCapacityPolicyVersion:
    policy = session.scalar(select(SourcePacingCapacityPolicyVersion).where(
        SourcePacingCapacityPolicyVersion.id == policy_id,
        SourcePacingCapacityPolicyVersion.tenant_id == tenant_id,
        SourcePacingCapacityPolicyVersion.pacing_domain == pacing_domain,
        SourcePacingCapacityPolicyVersion.status == "active",
    ))
    if policy is None:
        raise SourceCapacityConflict("source_capacity_policy_missing")
    return policy


def _group_slots(
    slots: list[SourcePacingSlot],
    points: dict[str, SourcePacingPoint],
) -> dict[str, list[SourcePacingSlot]]:
    grouped: dict[str, list[SourcePacingSlot]] = {}
    for slot in slots:
        if slot.slot_key not in points:
            continue
        source_hash = str(slot.pacing_source_key_hash or "")
        if not source_hash:
            raise SourceCapacityConflict("source_capacity_source_missing")
        grouped.setdefault(source_hash, []).append(slot)
    return grouped


def _capacity_scope(
    policy: SourcePacingCapacityPolicyVersion,
    pacing_domain: str,
    source_hash: str,
    *,
    slots: list[SourcePacingSlot],
) -> CapacityScope:
    return CapacityScope(
        tenant_id=policy.tenant_id,
        pacing_domain=pacing_domain,
        source_key_hash=source_hash,
        window_start_at=min(wall_datetime(slot.period_start_at) for slot in slots),
        window_end_at=max(wall_datetime(slot.deadline_at) for slot in slots),
        policy_version_id=policy.id,
        curve_hash=policy.content_hash,
    )


def _capacity_policy(policy: SourcePacingCapacityPolicyVersion) -> CapacityPolicy:
    return CapacityPolicy(
        hourly_curve=tuple(float(item) for item in (policy.hourly_curve or ())),
        minimum_gap_seconds=int(policy.minimum_gap_seconds),
        hourly_ceiling=int(policy.hourly_ceiling),
        headroom_floor=float(policy.headroom_floor),
        provider_retry_slots=int(policy.provider_retry_slots or 0),
    )


def _occupied_releases(slots: list[SourcePacingSlot]) -> tuple[datetime, ...]:
    values = {
        wall_datetime(value)
        for slot in slots
        for value in (slot.release_not_before_at, slot.historical_cursor_at)
        if value is not None
    }
    return tuple(sorted(values))


def _capacity_demands(
    slots: list[SourcePacingSlot],
    points: dict[str, SourcePacingPoint],
) -> tuple[CapacityDemand, ...]:
    return tuple(
        CapacityDemand(
            owner_id=slot.owner_id,
            earliest_at=wall_datetime(points[slot.slot_key].release_not_before_at),
            latest_at=wall_datetime(slot.deadline_at),
        )
        for slot in slots
        if slot.release_not_before_at is None
    )


def _apply_capacity_assignments(
    slots: list[SourcePacingSlot],
    points: dict[str, SourcePacingPoint],
    result: CapacityResult,
    *,
    adjusted: dict[str, SourcePacingPoint],
    enriched: dict[str, SourcePacingSlot],
    plan_hash: str,
) -> None:
    for slot in slots:
        ordinal = result.owner_slot_ordinals.get(slot.owner_id)
        if ordinal is None:
            enriched[slot.slot_key] = slot
            continue
        capacity_at = result.capacity_slots[ordinal - 1]
        point = points[slot.slot_key]
        adjusted[slot.slot_key] = SourcePacingPoint(
            due_at=point.due_at,
            release_not_before_at=max(point.release_not_before_at, capacity_at),
        )
        enriched[slot.slot_key] = replace(
            slot,
            source_capacity_plan_hash=plan_hash,
            source_capacity_slot_ordinal=ordinal,
        )


def plan_source_capacity(
    scope: CapacityScope,
    policy: CapacityPolicy,
    *,
    occupied_at: tuple[datetime, ...],
    demands: tuple[CapacityDemand, ...],
    baseline: CapacityBaseline | None = None,
) -> CapacityResult:
    _validate_inputs(scope, policy, demands)
    prior_slots = tuple(baseline.capacity_slots if baseline else ())
    occupied = tuple(sorted(set(occupied_at) | set(prior_slots)))
    incoming_count = len(demands) + int(baseline.incoming_count if baseline else 0)
    headroom = max(
        math.ceil(incoming_count * policy.headroom_floor),
        policy.provider_retry_slots,
    )
    prior_headroom = int(baseline.replacement_headroom if baseline else 0)
    additional_headroom = max(0, headroom - prior_headroom)
    requested = len(occupied) + len(demands) + additional_headroom
    hours = _window_hours(scope)
    quotas = _hour_quotas(hours, requested, policy)
    raw_slots = fit_candidate_slots(
        candidate_slots(scope, hours, quotas),
        demands,
        scope,
        occupied=occupied,
        minimum_gap_seconds=policy.minimum_gap_seconds,
    )
    available = _available_slots(raw_slots, occupied, policy.minimum_gap_seconds)
    assignments, owner_deficit = _assign_demands(available, demands)
    unused_count = len(available) - len(assignments)
    deficit = owner_deficit + max(0, additional_headroom - unused_count)
    slots, assignments = _aggregate_capacity_slots(occupied, available, assignments)
    admitted_assignments = assignments if deficit == 0 else {}
    return CapacityResult(
        capacity_slots=slots,
        owner_slot_ordinals=admitted_assignments,
        occupied_count=len(occupied),
        incoming_count=incoming_count,
        replacement_headroom=headroom,
        available_count=len(available),
        deficit_count=deficit,
        last_safe_release_at=min((item.latest_at for item in demands), default=None),
    )


def freeze_source_capacity_plan(
    session: Session,
    scope: CapacityScope,
    result: CapacityResult,
) -> SourcePacingCapacityPlan:
    lock_source_capacity(session, scope)
    plan_hash = _plan_hash(scope, result)
    existing = source_capacity_plan(session, scope, plan_hash=plan_hash)
    if existing is not None:
        return existing
    latest = source_capacity_plan(session, scope)
    revision = int(latest.revision if latest is not None else 0) + 1
    try:
        with session.begin_nested():
            plan = _capacity_plan(scope, result, plan_hash, revision=revision)
            session.add(plan)
            session.flush()
        return plan
    except IntegrityError as exc:
        winner = source_capacity_plan(session, scope, plan_hash=plan_hash)
        if winner is None:
            raise SourceCapacityConflict("source_capacity_plan_concurrent_conflict") from exc
        return winner


def _capacity_plan(
    scope: CapacityScope,
    result: CapacityResult,
    plan_hash: str,
    *,
    revision: int,
) -> SourcePacingCapacityPlan:
    return SourcePacingCapacityPlan(
        **asdict(scope),
        capacity_slots=[item.isoformat() for item in result.capacity_slots],
        occupied_count=result.occupied_count,
        incoming_count=result.incoming_count,
        replacement_headroom=result.replacement_headroom,
        available_count=result.available_count,
        deficit_count=result.deficit_count,
        last_safe_release_at=result.last_safe_release_at,
        state="frozen" if result.admitted else "insufficient",
        revision=revision,
        plan_hash=plan_hash,
    )


def _source_capacity_baseline(
    session: Session,
    scope: CapacityScope,
) -> CapacityBaseline | None:
    plans = latest_capacity_component_plans(session, scope)
    if not plans:
        return None
    return CapacityBaseline(
        capacity_slots=tuple(sorted({
            datetime.fromisoformat(str(item))
            for plan in plans
            for item in plan.capacity_slots
        })),
        incoming_count=sum(int(plan.incoming_count) for plan in plans),
        replacement_headroom=sum(int(plan.replacement_headroom) for plan in plans),
    )


def _validate_inputs(
    scope: CapacityScope,
    policy: CapacityPolicy,
    demands: tuple[CapacityDemand, ...],
) -> None:
    if scope.window_start_at >= scope.window_end_at:
        raise ValueError("source_capacity_window_invalid")
    if len(policy.hourly_curve) != HOURS_PER_DAY or any(value < 0 for value in policy.hourly_curve):
        raise ValueError("source_capacity_curve_invalid")
    if policy.minimum_gap_seconds < 1 or policy.hourly_ceiling < 1:
        raise ValueError("source_capacity_limit_invalid")
    if not 0 <= policy.headroom_floor <= 1 or policy.provider_retry_slots < 0:
        raise ValueError("source_capacity_headroom_invalid")
    if len({item.owner_id for item in demands}) != len(demands):
        raise ValueError("source_capacity_owner_duplicate")
    if any(item.earliest_at >= item.latest_at for item in demands):
        raise ValueError("source_capacity_deadline_invalid")


def _window_hours(scope: CapacityScope) -> tuple[datetime, ...]:
    cursor = scope.window_start_at.replace(minute=0, second=0, microsecond=0)
    hours: list[datetime] = []
    while cursor < scope.window_end_at:
        hours.append(cursor)
        cursor += timedelta(hours=1)
    return tuple(hours)


def _hour_quotas(
    hours: tuple[datetime, ...],
    requested: int,
    policy: CapacityPolicy,
) -> tuple[int, ...]:
    weights = [policy.hourly_curve[item.hour] for item in hours]
    total_weight = sum(weights)
    if requested == 0 or total_weight <= 0:
        return tuple(0 for _item in hours)
    raw = [requested * weight / total_weight for weight in weights]
    quotas = [min(policy.hourly_ceiling, math.floor(value)) for value in raw]
    remaining = requested - sum(quotas)
    order = sorted(range(len(hours)), key=lambda index: (raw[index] - quotas[index], -index), reverse=True)
    while remaining > 0 and any(quotas[index] < policy.hourly_ceiling for index in order):
        for index in order:
            if remaining == 0:
                break
            if weights[index] <= 0 or quotas[index] >= policy.hourly_ceiling:
                continue
            quotas[index] += 1
            remaining -= 1
    return tuple(quotas)


def _available_slots(
    candidates: tuple[datetime, ...],
    occupied: tuple[datetime, ...],
    minimum_gap_seconds: int,
) -> tuple[datetime, ...]:
    selected: list[datetime] = []
    blocked = tuple(sorted(occupied))
    for candidate in candidates:
        if any(abs((candidate - item).total_seconds()) < minimum_gap_seconds for item in blocked):
            continue
        if selected and (candidate - selected[-1]).total_seconds() < minimum_gap_seconds:
            continue
        selected.append(candidate)
    return tuple(selected)


def _assign_demands(
    slots: tuple[datetime, ...],
    demands: tuple[CapacityDemand, ...],
) -> tuple[dict[str, int], int]:
    available = set(range(len(slots)))
    assignments: dict[str, int] = {}
    for demand in sorted(demands, key=lambda item: (item.latest_at, item.earliest_at, item.owner_id)):
        ordinal = next((
            index for index in sorted(available)
            if demand.earliest_at <= slots[index] < demand.latest_at
        ), None)
        if ordinal is None:
            continue
        available.remove(ordinal)
        assignments[demand.owner_id] = ordinal + 1
    return assignments, len(demands) - len(assignments)


def _aggregate_capacity_slots(
    occupied: tuple[datetime, ...],
    available: tuple[datetime, ...],
    assignments: dict[str, int],
) -> tuple[tuple[datetime, ...], dict[str, int]]:
    slots = tuple(sorted(set(occupied) | set(available)))
    ordinals = {value: index for index, value in enumerate(slots, 1)}
    remapped = {
        owner_id: ordinals[available[ordinal - 1]]
        for owner_id, ordinal in assignments.items()
    }
    return slots, remapped


def _plan_hash(scope: CapacityScope, result: CapacityResult) -> str:
    payload = {
        "scope": asdict(scope),
        "capacity_slots": result.capacity_slots,
        "owner_slot_ordinals": result.owner_slot_ordinals,
        "occupied_count": result.occupied_count,
        "incoming_count": result.incoming_count,
        "replacement_headroom": result.replacement_headroom,
        "deficit_count": result.deficit_count,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "CapacityDemand",
    "CapacityBaseline",
    "CapacityPolicy",
    "CapacityResult",
    "CapacityScope",
    "SourceCapacityConflict",
    "apply_source_capacity_plan",
    "freeze_source_capacity_plan",
    "plan_source_capacity",
]
