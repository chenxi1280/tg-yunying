from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import (
    DispatchAllocationExclusion,
    DispatchClaimReservation,
    DispatchClaimShardAllocation,
    SearchClickAssignmentEpoch,
    SearchClickFulfillmentObligation,
)

from .dispatch_release_wave import start_or_join_dispatch_rebuild_wave
from .search_click_dispatch_allocation import SearchClickFulfillmentUnit
from .search_click_outcome_identity import (
    release_unit_set_hash,
    search_outcome_hash,
)
from .search_click_solver_snapshot import solver_component_hash_for_unit


def release_first_outcome_units(
    session: Session,
    epoch: SearchClickAssignmentEpoch,
    *,
    units: Sequence[SearchClickFulfillmentUnit],
    now_value: datetime,
    reason_code: str,
) -> list[dict]:
    release_facts = [
        _release_unit(
            session,
            epoch,
            unit=unit,
            reason_code=reason_code,
        )
        for unit in units
    ]
    if units:
        epoch.rebuild_input_version_after = start_or_join_dispatch_rebuild_wave(
            session,
            window_id=units[0].window_id,
            released_count=len(units),
            now_value=now_value,
        )
    return release_facts


def abandon_units(
    session: Session,
    epoch: SearchClickAssignmentEpoch,
    *,
    units: Sequence[SearchClickFulfillmentUnit],
    now_value: datetime,
    reason: str,
) -> None:
    unit_values = list(units)
    release_facts = release_first_outcome_units(
        session,
        epoch,
        units=unit_values,
        now_value=now_value,
        reason_code="search_solver_abandoned",
    )
    epoch.outcome = "abandoned"
    epoch.release_unit_set_hash = release_unit_set_hash(release_facts)
    epoch.released_unit_count = len(unit_values)
    epoch.outcome_hash = search_outcome_hash(
        epoch,
        solver_result={"outcome": "abandoned", "reason": reason},
        matches=(),
    )
    epoch.finalize_status = "finalized"
    epoch.finalized_at = now_value


def _release_unit(
    session: Session,
    epoch: SearchClickAssignmentEpoch,
    *,
    unit: SearchClickFulfillmentUnit,
    reason_code: str,
) -> dict:
    reservation = session.get(DispatchClaimReservation, unit.reservation_id)
    if reservation is None:
        raise RuntimeError("search_click_release_reservation_missing")
    allocation = session.get(
        DispatchClaimShardAllocation,
        reservation.dispatch_claim_shard_allocation_id,
    )
    available = (
        int(reservation.reserved_claims)
        - int(reservation.bound_count)
        - int(reservation.claimed_count)
        - int(reservation.released_count)
    )
    if (
        allocation is None
        or allocation.unclaimed_allocated_count <= 0
        or unit.fulfillment_lane_claim_ordinal > reservation.reserved_claims
        or available <= 0
    ):
        raise RuntimeError("dispatch_release_counter_invariant")
    component_hash = solver_component_hash_for_unit(
        session,
        epoch.id,
        unit.reservation_id,
        ordinal=unit.fulfillment_lane_claim_ordinal,
    )
    session.add(_release_exclusion(epoch, unit, reason_code, component_hash))
    reservation.released_count += 1
    reservation.version += 1
    allocation.unclaimed_allocated_count -= 1
    allocation.version += 1
    obligation = session.get(SearchClickFulfillmentObligation, unit.obligation_id)
    if obligation is not None:
        obligation.status = "open"
    return {
        "window_id": unit.window_id,
        "reservation_id": unit.reservation_id,
        "ordinal": unit.fulfillment_lane_claim_ordinal,
        "reason_code": reason_code,
        "resource_snapshot_hash": component_hash,
    }


def _release_exclusion(epoch, unit, reason_code, component_hash):
    return DispatchAllocationExclusion(
        dispatch_claim_window_id=unit.window_id,
        dispatch_claim_reservation_id=unit.reservation_id,
        fulfillment_lane_claim_ordinal=unit.fulfillment_lane_claim_ordinal,
        carrier_type="search_click_assignment_epoch",
        carrier_id=epoch.id,
        reason_code=reason_code,
        solver_problem_component_hash=component_hash,
        resource_snapshot_hash=component_hash,
    )


__all__ = ["abandon_units", "release_first_outcome_units"]
