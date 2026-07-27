from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Mapping
from zlib import crc32

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DispatchClaimReservation, DispatchClaimScope, DispatchClaimShardAllocation, DispatchClaimWindow

from .dispatch_claim_ledger import for_update, window_reservations
from .dispatch_claim_types import DispatchClaimDemand, PRIORITY_CLAIM_CLASSES, SHARED_CAPACITY_ERROR

# PRD §2.20.1 RC-4 claim 公平性：strict search_join 最小保留份额比例
SEARCH_JOIN_CLAIM_CLASS = "search_join"
MIN_RESERVED_CAPACITY_RATIO = 0.30
MIN_PRIORITY_CLAIM_SLOTS = 1


def allocate_window(
    session: Session,
    scope: DispatchClaimScope,
    window: DispatchClaimWindow,
    allocations: list[DispatchClaimShardAllocation],
    demands: list[DispatchClaimDemand],
) -> None:
    epoch = _next_allocation_epoch(window, allocations)
    _clear_unclaimed_reservations(session, window.id)
    available = max(0, int(scope.claim_capacity) - int(scope.active_claim_count))
    grants = _allocate_demands(demands, available, epoch, scope_capacity=int(scope.claim_capacity))
    _persist_allocations(session, window, allocations, demands, grants, epoch)


def strict_non_priority_demands(demands: list[DispatchClaimDemand]) -> list[DispatchClaimDemand]:
    return [demand for demand in demands if demand.is_strict and demand.claim_class not in PRIORITY_CLAIM_CLASSES]


def normal_demands(demands: list[DispatchClaimDemand]) -> list[DispatchClaimDemand]:
    return [demand for demand in demands if not demand.is_strict]


def rotated_demands(demands: list[DispatchClaimDemand], epoch: int) -> list[DispatchClaimDemand]:
    return sorted(demands, key=lambda demand: rotation_value(demand, epoch))


def rotation_value(demand: DispatchClaimDemand, epoch: int) -> int:
    value = ":".join(str(part) for part in (*demand.key, epoch))
    return crc32(value.encode("utf-8"))


def _next_allocation_epoch(window: DispatchClaimWindow, allocations: list[DispatchClaimShardAllocation]) -> int:
    if not allocations:
        return window.allocation_epoch
    window.allocation_epoch += 1
    window.version += 1
    return window.allocation_epoch


def _clear_unclaimed_reservations(session: Session, window_id: str) -> None:
    for reservation in window_reservations(session, window_id).values():
        if reservation.reserved_claims != reservation.claimed_count:
            raise RuntimeError("cannot replace dispatch allocation with unclaimed reservations")
        reservation.required_claims = 0
        if reservation.reason != "unclaimed_action_no_longer_due":
            reservation.reason = "allocation_epoch_replaced"
        reservation.version += 1


def _allocate_demands(
    demands: list[DispatchClaimDemand],
    available: int,
    epoch: int,
    *,
    scope_capacity: int = 0,
) -> dict[tuple[int, str, str, int, int], int]:
    grants = {demand.key: 0 for demand in demands}
    ordinary = normal_demands(demands)
    ordinary_reserve = _ordinary_first_share_reserve(ordinary, available)
    remaining = available - ordinary_reserve
    remaining = _allocate_strict_search_join_reserved(demands, grants, remaining, epoch, scope_capacity)
    remaining = _allocate_priority_demands(demands, grants, remaining, epoch)
    remaining = _allocate_balanced_demands(strict_non_priority_demands(demands), grants, remaining, epoch)
    _allocate_balanced_demands(ordinary, grants, remaining + ordinary_reserve, epoch)
    return grants


def _ordinary_first_share_reserve(
    demands: list[DispatchClaimDemand],
    available: int,
) -> int:
    required = sum(1 for demand in demands if demand.required_claims > 0)
    return min(required, max(0, available - MIN_PRIORITY_CLAIM_SLOTS))


def _allocate_strict_search_join_reserved(
    demands: list[DispatchClaimDemand],
    grants: dict[tuple[int, str, str, int, int], int],
    available: int,
    epoch: int,
    scope_capacity: int,
) -> int:
    """PRD §2.20.1: 给 strict search_join 预留 min_reserved_capacity，防止 hard_hourly 饿死。"""
    search_join_demands = [
        demand for demand in demands
        if demand.claim_class == SEARCH_JOIN_CLAIM_CLASS and demand.is_strict
    ]
    if not search_join_demands or available <= 0:
        return available
    base = scope_capacity if scope_capacity > 0 else available
    min_reserved = max(1, int(base * MIN_RESERVED_CAPACITY_RATIO))
    reserve_budget = min(available, min_reserved)
    unused = _allocate_in_order(search_join_demands, grants, reserve_budget, epoch)
    return available - reserve_budget + unused


def _allocate_priority_demands(
    demands: list[DispatchClaimDemand],
    grants: dict[tuple[int, str, str, int, int], int],
    available: int,
    epoch: int,
) -> int:
    rows = [
        demand
        for demand in demands
        if demand.claim_class in PRIORITY_CLAIM_CLASSES and demand.is_strict
    ]
    return _allocate_balanced_demands(rows, grants, available, epoch)


def _allocate_in_order(
    demands: list[DispatchClaimDemand],
    grants: dict[tuple[int, str, str, int, int], int],
    available: int,
    epoch: int,
) -> int:
    remaining = available
    for demand in rotated_demands(demands, epoch):
        assigned = min(demand.required_claims, remaining)
        grants[demand.key] += assigned
        remaining -= assigned
        if remaining == 0:
            break
    return remaining


def _allocate_balanced_demands(
    demands: list[DispatchClaimDemand],
    grants: dict[tuple[int, str, str, int, int], int],
    available: int,
    epoch: int,
) -> int:
    remaining = _allocate_first_share(demands, grants, available, epoch)
    while remaining > 0:
        demand = _best_weighted_demand(demands, grants, epoch)
        if demand is None:
            return remaining
        grants[demand.key] += 1
        remaining -= 1
    return remaining


def _allocate_first_share(
    demands: list[DispatchClaimDemand],
    grants: dict[tuple[int, str, str, int, int], int],
    available: int,
    epoch: int,
) -> int:
    remaining = available
    for demand in rotated_demands(demands, epoch):
        if remaining == 0:
            break
        if demand.required_claims > 0 and grants[demand.key] < demand.required_claims:
            grants[demand.key] += 1
            remaining -= 1
    return remaining


def _best_weighted_demand(
    demands: list[DispatchClaimDemand],
    grants: dict[tuple[int, str, str, int, int], int],
    epoch: int,
) -> DispatchClaimDemand | None:
    eligible = [demand for demand in demands if grants[demand.key] < demand.required_claims]
    if not eligible:
        return None
    return max(eligible, key=lambda demand: _weighted_order_key(demand, grants[demand.key], epoch))


def _weighted_order_key(demand: DispatchClaimDemand, granted: int, epoch: int) -> tuple[float, int]:
    return (float(demand.urgency_score) / float(granted + 1), -rotation_value(demand, epoch))


def _persist_allocations(
    session: Session,
    window: DispatchClaimWindow,
    allocations: list[DispatchClaimShardAllocation],
    demands: list[DispatchClaimDemand],
    grants: Mapping[tuple[int, str, str, int, int], int],
    epoch: int,
) -> None:
    allocation_map = _allocation_map(allocations)
    for demand in demands:
        allocation = _allocation_for_demand(session, window, allocation_map, demand)
        reservation = _reservation_for_demand(session, allocation, demand, window.bucket_start)
        _write_reservation(reservation, demand, int(grants.get(demand.key, 0)))
    _write_allocation_totals(window, allocation_map, demands, grants, epoch)


def _allocation_map(
    allocations: list[DispatchClaimShardAllocation],
) -> dict[tuple[int, int], DispatchClaimShardAllocation]:
    return {(row.account_shard_total, row.account_shard_index): row for row in allocations}


def _allocation_for_demand(
    session: Session,
    window: DispatchClaimWindow,
    allocations: dict[tuple[int, int], DispatchClaimShardAllocation],
    demand: DispatchClaimDemand,
) -> DispatchClaimShardAllocation:
    key = (demand.shard_total, demand.shard_index)
    allocation = allocations.get(key)
    if allocation is not None:
        return allocation
    allocation = DispatchClaimShardAllocation(
        dispatch_claim_window_id=window.id,
        account_shard_total=demand.shard_total,
        account_shard_index=demand.shard_index,
    )
    session.add(allocation)
    session.flush()
    allocations[key] = allocation
    return allocation


def _reservation_for_demand(
    session: Session,
    allocation: DispatchClaimShardAllocation,
    demand: DispatchClaimDemand,
    bucket_start: datetime,
) -> DispatchClaimReservation:
    statement = select(DispatchClaimReservation).where(
        DispatchClaimReservation.dispatch_claim_shard_allocation_id == allocation.id,
        DispatchClaimReservation.tenant_id == demand.tenant_id,
        DispatchClaimReservation.task_id == demand.task_id,
        DispatchClaimReservation.claim_class == demand.claim_class,
    )
    reservation = session.scalar(for_update(session, statement))
    if reservation is not None:
        return reservation
    reservation = DispatchClaimReservation(
        dispatch_claim_shard_allocation_id=allocation.id,
        tenant_id=demand.tenant_id,
        task_id=demand.task_id,
        claim_class=demand.claim_class,
        bucket_start=bucket_start,
    )
    session.add(reservation)
    session.flush()
    return reservation


def _write_reservation(reservation: DispatchClaimReservation, demand: DispatchClaimDemand, grant: int) -> None:
    reservation.required_claims = demand.required_claims
    reservation.reserved_claims = reservation.claimed_count + grant
    reservation.urgency_score = demand.urgency_score
    reservation.reason = "allocated" if grant >= demand.required_claims else SHARED_CAPACITY_ERROR
    reservation.version += 1


def _write_allocation_totals(
    window: DispatchClaimWindow,
    allocations: Mapping[tuple[int, int], DispatchClaimShardAllocation],
    demands: list[DispatchClaimDemand],
    grants: Mapping[tuple[int, str, str, int, int], int],
    epoch: int,
) -> None:
    by_shard: dict[tuple[int, int], list[DispatchClaimDemand]] = defaultdict(list)
    for demand in demands:
        by_shard[(demand.shard_total, demand.shard_index)].append(demand)
    for key, rows in by_shard.items():
        allocation = allocations[key]
        allocation.required_claims = sum(demand.required_claims for demand in rows)
        allocation.unclaimed_allocated_count = sum(int(grants[demand.key]) for demand in rows)
        allocation.reason = "allocated" if allocation.unclaimed_allocated_count else SHARED_CAPACITY_ERROR
        allocation.version += 1
    window.unclaimed_allocated_count = sum(row.unclaimed_allocated_count for row in allocations.values())
    window.allocation_epoch = epoch
    window.version += 1
