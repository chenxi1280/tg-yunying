from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
import json
from typing import Mapping
from zlib import crc32

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    DispatchClaimReservation,
    DispatchClaimScope,
    DispatchClaimShardAllocation,
    DispatchClaimTaskAllocation,
    DispatchClaimWindow,
)
from .dispatch_rebuild_snapshot import CONTRACT_VERSION, dispatch_rebuild_snapshot_hash

from .dispatch_claim_ledger import for_update
from .dispatch_claim_types import (
    DispatchClaimDemand,
    PRIORITY_CLAIM_CLASSES,
    SHARED_CAPACITY_ERROR,
)


def allocate_window(
    session: Session,
    scope: DispatchClaimScope,
    window: DispatchClaimWindow,
    allocations: list[DispatchClaimShardAllocation],
    demands: list[DispatchClaimDemand],
    *,
    live_shard_available: Mapping[int, int] | None = None,
    runtime_shard_total: int | None = None,
) -> None:
    if window.allocation_state == "ready":
        return
    epoch = _next_allocation_epoch(window, allocations)
    opportunity_cursor = int(scope.opportunity_cursor) + 1
    window.rebuild_input_hash = dispatch_rebuild_snapshot_hash(
        session,
        scope,
        window,
        demands,
        allocations,
    )
    available = max(
        0,
        int(scope.claim_capacity)
        - int(scope.active_claim_count)
        - int(window.effective_unclaimed_count),
    )
    if live_shard_available is not None:
        available = min(available, sum(live_shard_available.values()))
    grants = _allocate_demands_with_live_limits(
        demands,
        available,
        opportunity_cursor,
        live_shard_available=live_shard_available,
        runtime_shard_total=runtime_shard_total,
    )
    _persist_allocations(
        session,
        window,
        allocations,
        demands,
        grants,
        epoch,
        opportunity_cursor,
        contract_version=(
            scope.active_contract_version
            or scope.candidate_contract_version
            or CONTRACT_VERSION
        ),
    )
    scope.opportunity_cursor = opportunity_cursor
    scope.version += 1
    window.allocation_state = "ready"
    window.ready_rebuild_snapshot_hash = dispatch_demand_hash(demands)
    window.pending_rebuild_release_count = 0
    window.allocation_scope_version = scope.version
    window.allocation_scope_active_count = scope.active_claim_count


def request_window_rebuild(
    window: DispatchClaimWindow,
    *,
    released_count: int,
    rebuild_input_hash: str,
    input_changed: bool = False,
) -> bool:
    if released_count <= 0 and not input_changed:
        return False
    window.allocation_state = "rebuild_required"
    window.rebuild_input_hash = rebuild_input_hash
    window.pending_rebuild_release_count = max(
        int(window.pending_rebuild_release_count or 0),
        released_count,
    )
    window.version += 1
    return True


def dispatch_demand_hash(demands: list[DispatchClaimDemand]) -> str:
    payload = [
        {
            "key": demand.key,
            "business_task_id": demand.business_task_id,
            "lane_business_kind": demand.lane_business_kind,
            "action_ids": demand.action_ids,
            "required_claims": demand.required_claims,
            "urgency_score": demand.urgency_score,
            "strict": demand.is_strict,
        }
        for demand in sorted(demands, key=lambda item: item.key)
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def dispatch_rebuild_input_hash(
    scope: DispatchClaimScope,
    window: DispatchClaimWindow,
    demands: list[DispatchClaimDemand],
    *,
    released_count: int,
) -> str:
    payload = {
        "scope": {
            "id": scope.id,
            "version": scope.version,
            "claim_capacity": scope.claim_capacity,
            "active_claim_count": scope.active_claim_count,
            "opportunity_cursor": scope.opportunity_cursor,
            "runtime_shard_total": scope.runtime_shard_total,
            "topology_fingerprint": scope.topology_fingerprint,
            "capacity_config_fingerprint": scope.capacity_config_fingerprint,
        },
        "window": {
            "id": window.id,
            "version": window.version,
            "dispatch_allocation_epoch": window.allocation_epoch,
            "effective_unclaimed_count": window.effective_unclaimed_count,
        },
        "released_count": released_count,
        "demands": [
            {
                "key": demand.key,
                "business_task_id": demand.business_task_id,
                "lane_business_kind": demand.lane_business_kind,
                "action_ids": demand.action_ids,
                "required_claims": demand.required_claims,
                "urgency_score": demand.urgency_score,
            }
            for demand in sorted(demands, key=lambda item: item.key)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def strict_non_priority_demands(
    demands: list[DispatchClaimDemand],
) -> list[DispatchClaimDemand]:
    return [demand for demand in demands if demand.is_strict and demand.claim_class not in PRIORITY_CLAIM_CLASSES]


def normal_demands(demands: list[DispatchClaimDemand]) -> list[DispatchClaimDemand]:
    return [demand for demand in demands if not demand.is_strict]


def rotated_demands(demands: list[DispatchClaimDemand], epoch: int) -> list[DispatchClaimDemand]:
    return sorted(demands, key=lambda demand: rotation_value(demand, epoch))


def rotation_value(demand: DispatchClaimDemand, epoch: int) -> int:
    value = ":".join(str(part) for part in (*demand.key, epoch))
    return crc32(value.encode("utf-8"))


def _next_allocation_epoch(window: DispatchClaimWindow, allocations: list[DispatchClaimShardAllocation]) -> int:
    if not any(
        row.dispatch_allocation_epoch == window.allocation_epoch
        for row in allocations
    ):
        return window.allocation_epoch
    window.allocation_epoch += 1
    window.version += 1
    return window.allocation_epoch


def _allocate_demands(
    demands: list[DispatchClaimDemand],
    available: int,
    epoch: int,
    *,
    scope_capacity: int = 0,
) -> dict[tuple[int, str, str, int, int], int]:
    grants = {demand.key: 0 for demand in demands}
    del scope_capacity
    remaining = _allocate_parent_first_share(demands, grants, available, epoch)
    _allocate_balanced_demands(demands, grants, remaining, epoch)
    return grants


def _allocate_demands_with_live_limits(
    demands: list[DispatchClaimDemand],
    available: int,
    epoch: int,
    *,
    live_shard_available: Mapping[int, int] | None,
    runtime_shard_total: int | None,
) -> dict[tuple[int, str, str, int, int], int]:
    if live_shard_available is None or runtime_shard_total is None:
        return _allocate_demands(demands, available, epoch)
    grants = {demand.key: 0 for demand in demands}
    shard_budget = dict(live_shard_available)
    remaining = _allocate_limited_parent_share(
        demands, grants, shard_budget, available, epoch, runtime_shard_total,
    )
    while remaining > 0:
        eligible = [
            demand for demand in demands
            if _limited_demand_available(
                demand, grants, shard_budget, runtime_shard_total,
            )
        ]
        if not eligible:
            break
        demand = min(
            eligible,
            key=lambda row: (
                grants[row.key] / max(1, row.required_claims),
                -row.urgency_score,
                rotation_value(row, epoch),
            ),
        )
        _grant_limited(demand, grants, shard_budget, runtime_shard_total)
        remaining -= 1
    return grants


def _allocate_limited_parent_share(
    demands: list[DispatchClaimDemand],
    grants: dict[tuple[int, str, str, int, int], int],
    shard_budget: dict[int, int],
    available: int,
    epoch: int,
    runtime_shard_total: int,
) -> int:
    parents: dict[tuple[int, str], list[DispatchClaimDemand]] = defaultdict(list)
    for demand in demands:
        if demand.required_claims > 0:
            parents[(demand.tenant_id, demand.business_task_id)].append(demand)
    remaining = available
    for parent in sorted(parents, key=lambda key: _parent_rotation(key, epoch)):
        eligible = [
            demand for demand in parents[parent]
            if _limited_demand_available(
                demand, grants, shard_budget, runtime_shard_total,
            )
        ]
        if remaining <= 0:
            break
        if not eligible:
            continue
        demand = min(eligible, key=lambda row: rotation_value(row, epoch))
        _grant_limited(demand, grants, shard_budget, runtime_shard_total)
        remaining -= 1
    return remaining


def _limited_demand_available(
    demand: DispatchClaimDemand,
    grants: Mapping[tuple[int, str, str, int, int], int],
    shard_budget: Mapping[int, int],
    runtime_shard_total: int,
) -> bool:
    if grants[demand.key] >= demand.required_claims:
        return False
    if demand.shard_total != runtime_shard_total:
        return True
    return shard_budget.get(demand.shard_index, 0) > 0


def _grant_limited(
    demand: DispatchClaimDemand,
    grants: dict[tuple[int, str, str, int, int], int],
    shard_budget: dict[int, int],
    runtime_shard_total: int,
) -> None:
    grants[demand.key] += 1
    if demand.shard_total == runtime_shard_total:
        shard_budget[demand.shard_index] -= 1


def _allocate_parent_first_share(
    demands: list[DispatchClaimDemand],
    grants: dict[tuple[int, str, str, int, int], int],
    available: int,
    epoch: int,
) -> int:
    parents: dict[tuple[int, str], list[DispatchClaimDemand]] = defaultdict(list)
    for demand in demands:
        if demand.required_claims > 0:
            parents[(demand.tenant_id, demand.business_task_id)].append(demand)
    remaining = available
    for parent_key in sorted(parents, key=lambda key: _parent_rotation(key, epoch)):
        if remaining == 0:
            break
        demand = min(
            parents[parent_key],
            key=lambda item: rotation_value(item, epoch),
        )
        grants[demand.key] += 1
        remaining -= 1
    return remaining


def _allocate_balanced_demands(
    demands: list[DispatchClaimDemand],
    grants: dict[tuple[int, str, str, int, int], int],
    available: int,
    epoch: int,
) -> int:
    unmet = {
        demand.key: max(0, demand.required_claims - grants[demand.key])
        for demand in demands
    }
    total_unmet = sum(unmet.values())
    budget = min(available, total_unmet)
    if budget == 0:
        return available
    bases: dict[tuple[int, str, str, int, int], int] = {}
    for demand in demands:
        bases[demand.key] = (budget * unmet[demand.key]) // total_unmet
        grants[demand.key] += bases[demand.key]
    distributed = sum(bases.values())
    leftover = budget - distributed
    for demand in _largest_remainder_order(demands, unmet, budget, total_unmet, epoch):
        if leftover == 0:
            break
        if grants[demand.key] < demand.required_claims:
            grants[demand.key] += 1
            leftover -= 1
    return available - budget


def _largest_remainder_order(
    demands: list[DispatchClaimDemand],
    unmet: Mapping[tuple[int, str, str, int, int], int],
    budget: int,
    total_unmet: int,
    epoch: int,
) -> list[DispatchClaimDemand]:
    return sorted(
        demands,
        key=lambda demand: (
            -((budget * unmet[demand.key]) % total_unmet),
            -demand.urgency_score,
            rotation_value(demand, epoch),
        ),
    )


def _parent_rotation(parent_key: tuple[int, str], epoch: int) -> int:
    return crc32(f"{parent_key[0]}:{parent_key[1]}:{epoch}".encode("utf-8"))


def _persist_allocations(
    session: Session,
    window: DispatchClaimWindow,
    allocations: list[DispatchClaimShardAllocation],
    demands: list[DispatchClaimDemand],
    grants: Mapping[tuple[int, str, str, int, int], int],
    epoch: int,
    opportunity_cursor: int,
    contract_version: str,
) -> None:
    allocation_map = _allocation_map(allocations, epoch)
    task_allocation_map: dict[tuple[int, str, str], DispatchClaimTaskAllocation] = {}
    for demand in demands:
        task_allocation = _task_allocation_for_demand(
            session,
            window,
            task_allocation_map,
            demand,
            epoch,
            opportunity_cursor,
        )
        allocation = _allocation_for_demand(
            session,
            window,
            allocation_map,
            demand,
            epoch,
            contract_version,
        )
        reservation = _reservation_for_demand(
            session,
            task_allocation,
            allocation,
            demand,
            window.bucket_start,
            epoch,
        )
        _write_reservation(reservation, demand, int(grants.get(demand.key, 0)))
    _write_task_allocation_totals(task_allocation_map, demands, grants)
    _write_allocation_totals(
        window,
        allocation_map,
        allocations,
        demands,
        grants,
        epoch,
    )


def _allocation_map(
    allocations: list[DispatchClaimShardAllocation],
    epoch: int,
) -> dict[tuple[int, int], DispatchClaimShardAllocation]:
    return {
        (row.account_shard_total, row.account_shard_index): row
        for row in allocations
        if row.dispatch_allocation_epoch == epoch
    }


def _task_allocation_for_demand(
    session: Session,
    window: DispatchClaimWindow,
    allocations: dict[tuple[int, str, str], DispatchClaimTaskAllocation],
    demand: DispatchClaimDemand,
    epoch: int,
    opportunity_cursor: int,
) -> DispatchClaimTaskAllocation:
    key = (
        demand.tenant_id,
        demand.business_task_id,
        demand.lane_business_kind,
    )
    allocation = allocations.get(key)
    if allocation is not None:
        return allocation
    allocation = DispatchClaimTaskAllocation(
        dispatch_claim_window_id=window.id,
        dispatch_allocation_epoch=epoch,
        tenant_id=demand.tenant_id,
        allocation_business_task_id=demand.business_task_id,
        lane_business_kind=demand.lane_business_kind,
        opportunity_cursor_snapshot=opportunity_cursor,
        rebuild_input_hash=window.rebuild_input_hash,
        dispatch_rebuild_snapshot_hash=window.rebuild_input_hash,
    )
    session.add(allocation)
    session.flush()
    allocations[key] = allocation
    return allocation


def _allocation_for_demand(
    session: Session,
    window: DispatchClaimWindow,
    allocations: dict[tuple[int, int], DispatchClaimShardAllocation],
    demand: DispatchClaimDemand,
    epoch: int,
    contract_version: str,
) -> DispatchClaimShardAllocation:
    key = (demand.shard_total, demand.shard_index)
    allocation = allocations.get(key)
    if allocation is not None:
        return allocation
    allocation = DispatchClaimShardAllocation(
        dispatch_claim_window_id=window.id,
        dispatch_allocation_epoch=epoch,
        rebuild_input_hash=window.rebuild_input_hash,
        dispatch_rebuild_snapshot_hash=window.rebuild_input_hash,
        dispatch_contract_version=contract_version,
        account_shard_total=demand.shard_total,
        account_shard_index=demand.shard_index,
    )
    session.add(allocation)
    session.flush()
    allocations[key] = allocation
    return allocation


def _reservation_for_demand(
    session: Session,
    task_allocation: DispatchClaimTaskAllocation,
    allocation: DispatchClaimShardAllocation,
    demand: DispatchClaimDemand,
    bucket_start: datetime,
    epoch: int,
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
        dispatch_claim_task_allocation_id=task_allocation.id,
        dispatch_allocation_epoch=epoch,
        rebuild_input_hash=task_allocation.rebuild_input_hash,
        dispatch_rebuild_snapshot_hash=task_allocation.dispatch_rebuild_snapshot_hash,
        tenant_id=demand.tenant_id,
        task_id=demand.task_id,
        claim_class=demand.claim_class,
        bucket_start=bucket_start,
    )
    session.add(reservation)
    session.flush()
    return reservation


def _write_reservation(
    reservation: DispatchClaimReservation,
    demand: DispatchClaimDemand,
    grant: int,
) -> None:
    reservation.required_claims = demand.required_claims
    reservation.reserved_claims = (
        reservation.claimed_count
        + reservation.bound_count
        + reservation.released_count
        + grant
    )
    reservation.urgency_score = demand.urgency_score
    reservation.reason = "allocated" if grant > 0 else SHARED_CAPACITY_ERROR
    reservation.version += 1


def _write_task_allocation_totals(
    allocations: Mapping[tuple[int, str, str], DispatchClaimTaskAllocation],
    demands: list[DispatchClaimDemand],
    grants: Mapping[tuple[int, str, str, int, int], int],
) -> None:
    grouped: dict[tuple[int, str, str], list[DispatchClaimDemand]] = defaultdict(list)
    for demand in demands:
        key = (
            demand.tenant_id,
            demand.business_task_id,
            demand.lane_business_kind,
        )
        grouped[key].append(demand)
    for key, rows in grouped.items():
        allocation = allocations[key]
        allocation.required_claims = sum(row.required_claims for row in rows)
        allocation.reserved_claims = sum(int(grants[row.key]) for row in rows)
        allocation.urgency_score = max(row.urgency_score for row in rows)


def _write_allocation_totals(
    window: DispatchClaimWindow,
    allocations: Mapping[tuple[int, int], DispatchClaimShardAllocation],
    prior_allocations: list[DispatchClaimShardAllocation],
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
    all_allocations = {row.id: row for row in prior_allocations}
    all_allocations.update({row.id: row for row in allocations.values()})
    window.unclaimed_allocated_count = sum(
        row.unclaimed_allocated_count for row in all_allocations.values()
    )
    window.effective_unclaimed_count = window.unclaimed_allocated_count
    window.allocation_epoch = epoch
    window.version += 1
