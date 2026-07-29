from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    DispatchAllocationExclusion,
    DispatchClaimReservation,
    DispatchClaimScope,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
)

from .dispatch_claim_types import DispatchClaimDemand

CONTRACT_VERSION = "dispatch-rebuild-v2"


def dispatch_rebuild_snapshot_hash(
    session: Session,
    scope: DispatchClaimScope,
    window: DispatchClaimWindow,
    demands: list[DispatchClaimDemand],
    allocations: list[DispatchClaimShardAllocation],
) -> str:
    payload = {
        "dispatch_rebuild_contract_version": CONTRACT_VERSION,
        "scope": _scope_snapshot(scope),
        "window": _window_snapshot(window),
        "shards": [_shard_snapshot(row) for row in sorted(
            allocations, key=lambda item: (item.dispatch_allocation_epoch, item.id)
        )],
        "demands": [_demand_snapshot(row) for row in sorted(
            demands, key=lambda item: item.key
        )],
        "reservations": _window_reservation_snapshot(session, window.id),
        "active_exclusions": _active_exclusion_snapshot(session, window.id),
    }
    return _hash_payload(payload)


def _scope_snapshot(scope: DispatchClaimScope) -> dict:
    return {
        "id": scope.id,
        "version": scope.version,
        "claim_capacity": scope.claim_capacity,
        "active_claim_count": scope.active_claim_count,
        "opportunity_cursor": scope.opportunity_cursor,
    }


def _window_snapshot(window: DispatchClaimWindow) -> dict:
    return {
        "id": window.id,
        "version": window.version,
        "dispatch_allocation_epoch": window.allocation_epoch,
        "rebuild_input_version": window.rebuild_input_version,
        "claim_capacity": window.claim_capacity,
        "active_claim_count": window.active_claim_count,
        "unclaimed_allocated_count": window.unclaimed_allocated_count,
    }


def _shard_snapshot(row: DispatchClaimShardAllocation) -> dict:
    return {
        "id": row.id,
        "version": row.version,
        "epoch": row.dispatch_allocation_epoch,
        "shard_total": row.account_shard_total,
        "shard_index": row.account_shard_index,
        "active": row.active_claim_count,
        "unclaimed": row.unclaimed_allocated_count,
    }


def _demand_snapshot(demand: DispatchClaimDemand) -> dict:
    return {
        "key": demand.key,
        "business_task_id": demand.business_task_id,
        "lane_business_kind": demand.lane_business_kind,
        "action_ids": demand.action_ids,
        "required_claims": demand.required_claims,
        "urgency_score": demand.urgency_score,
        "strict": demand.is_strict,
    }


def _window_reservation_snapshot(session: Session, window_id: str) -> list[dict]:
    rows = session.execute(
        select(DispatchClaimReservation, DispatchClaimShardAllocation)
        .join(
            DispatchClaimShardAllocation,
            DispatchClaimShardAllocation.id
            == DispatchClaimReservation.dispatch_claim_shard_allocation_id,
        )
        .where(DispatchClaimShardAllocation.dispatch_claim_window_id == window_id)
        .order_by(DispatchClaimReservation.id)
    ).all()
    return [{
        "id": reservation.id,
        "version": reservation.version,
        "epoch": reservation.dispatch_allocation_epoch,
        "reserved": reservation.reserved_claims,
        "bound": reservation.bound_count,
        "claimed": reservation.claimed_count,
        "released": reservation.released_count,
    } for reservation, _ in rows]


def _active_exclusion_snapshot(session: Session, window_id: str) -> list[dict]:
    rows = session.scalars(
        select(DispatchAllocationExclusion)
        .where(
            DispatchAllocationExclusion.dispatch_claim_window_id == window_id,
            DispatchAllocationExclusion.state == "active",
        )
        .order_by(
            DispatchAllocationExclusion.dispatch_claim_reservation_id,
            DispatchAllocationExclusion.fulfillment_lane_claim_ordinal,
        )
    )
    return [{
        "reservation_id": row.dispatch_claim_reservation_id,
        "ordinal": row.fulfillment_lane_claim_ordinal,
        "reason": row.reason_code,
        "resource_snapshot_hash": row.resource_snapshot_hash,
    } for row in rows]


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["CONTRACT_VERSION", "dispatch_rebuild_snapshot_hash"]
