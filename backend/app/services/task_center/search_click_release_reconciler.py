from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    DispatchAllocationExclusion,
    DispatchAllocationReleaseBatch,
    DispatchAllocationReleaseBatchItem,
    DispatchClaimReservation,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    ExecutionAttempt,
    SearchClickFulfillmentObligation,
    SearchClickOpportunityAssignment,
)


def reconcile_complete_release(
    session: Session,
    assignment: SearchClickOpportunityAssignment,
    *,
    exclusion: DispatchAllocationExclusion,
    now_value: datetime,
) -> tuple[str, str]:
    batch = _validate_release_carrier(session, assignment, exclusion)
    if _gateway_started(session, assignment):
        raise RuntimeError("release_claim_fact_conflict")
    _align_assignment(
        session,
        assignment,
        reason_code=exclusion.reason_code,
        now_value=now_value,
    )
    reservation = session.get(
        DispatchClaimReservation,
        assignment.dispatch_claim_reservation_id,
    )
    if reservation is None:
        raise RuntimeError("release_fact_incomplete")
    _recount_reservation(session, reservation)
    recount_dispatch_unclaimed(session, reservation)
    return exclusion.carrier_type, batch.id


def _validate_release_carrier(
    session: Session,
    assignment: SearchClickOpportunityAssignment,
    exclusion: DispatchAllocationExclusion,
) -> DispatchAllocationReleaseBatch:
    if exclusion.carrier_type != "dispatch_allocation_release_batch":
        raise RuntimeError("release_fact_incomplete")
    batch = session.get(DispatchAllocationReleaseBatch, exclusion.carrier_id)
    if batch is None or batch.finalize_status != "finalized":
        raise RuntimeError("release_fact_incomplete")
    items = list(session.scalars(select(
        DispatchAllocationReleaseBatchItem
    ).where(
        DispatchAllocationReleaseBatchItem.release_batch_id == batch.id
    )))
    counts = {
        "released": batch.release_unit_count,
        "already_released": batch.already_released_unit_count,
        "precondition_lost": batch.precondition_lost_unit_count,
    }
    observed = {
        key: sum(item.classification == key for item in items)
        for key in counts
    }
    matching = any(_matching_released_item(item, assignment) for item in items)
    complete = (
        len(items) == batch.candidate_unit_count
        and sum(counts.values()) == batch.candidate_unit_count
        and counts == observed
        and matching
        and bool(batch.release_unit_set_hash)
        and bool(batch.outcome_hash)
    )
    if not complete:
        raise RuntimeError("release_fact_incomplete")
    return batch


def _matching_released_item(
    item: DispatchAllocationReleaseBatchItem,
    assignment: SearchClickOpportunityAssignment,
) -> bool:
    return (
        item.assignment_id == assignment.id
        and item.dispatch_claim_reservation_id
        == assignment.dispatch_claim_reservation_id
        and item.fulfillment_lane_claim_ordinal
        == assignment.fulfillment_lane_claim_ordinal
        and item.classification == "released"
    )


def _gateway_started(
    session: Session,
    assignment: SearchClickOpportunityAssignment,
) -> bool:
    if not assignment.action_id:
        return False
    return session.scalar(select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == assignment.action_id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).limit(1)) is not None


def _align_assignment(
    session: Session,
    assignment: SearchClickOpportunityAssignment,
    *,
    reason_code: str,
    now_value: datetime,
) -> None:
    if assignment.state == "released":
        return
    assignment.state = "released"
    assignment.release_reason = reason_code
    assignment.version += 1
    action = session.get(Action, assignment.action_id) if assignment.action_id else None
    if action is not None:
        action.status = "skipped"
        action.executed_at = now_value
        action.result = {
            **(action.result or {}),
            "success": False,
            "error_code": reason_code,
            "dispatch_prebound": False,
            "dispatch_claim_active": False,
        }
    obligation = session.get(
        SearchClickFulfillmentObligation,
        assignment.obligation_id,
    )
    if obligation is not None and obligation.status == "action_bound":
        obligation.status = "open"


def _recount_reservation(
    session: Session,
    reservation: DispatchClaimReservation,
) -> None:
    assignments = list(session.scalars(select(
        SearchClickOpportunityAssignment
    ).where(
        SearchClickOpportunityAssignment.dispatch_claim_reservation_id
        == reservation.id
    )))
    exclusions = set(session.scalars(select(
        DispatchAllocationExclusion.fulfillment_lane_claim_ordinal
    ).where(
        DispatchAllocationExclusion.dispatch_claim_reservation_id
        == reservation.id
    )))
    bound = sum(
        row.state in {"reserved", "action_bound"}
        and row.fulfillment_lane_claim_ordinal not in exclusions
        for row in assignments
    )
    claimed = sum(
        row.state in {"claimed", "gateway_started", "unknown", "consumed"}
        and row.fulfillment_lane_claim_ordinal not in exclusions
        for row in assignments
    )
    if bound + claimed + len(exclusions) > reservation.reserved_claims:
        raise RuntimeError("release_fact_incomplete")
    reservation.bound_count = bound
    reservation.claimed_count = claimed
    reservation.released_count = len(exclusions)
    reservation.version += 1


def recount_dispatch_unclaimed(
    session: Session,
    reservation: DispatchClaimReservation,
) -> None:
    allocation = session.get(
        DispatchClaimShardAllocation,
        reservation.dispatch_claim_shard_allocation_id,
    )
    if allocation is None:
        raise RuntimeError("release_fact_incomplete")
    reservations = session.scalars(select(DispatchClaimReservation).where(
        DispatchClaimReservation.dispatch_claim_shard_allocation_id
        == allocation.id
    ))
    allocation.unclaimed_allocated_count = sum(
        _reservation_unclaimed_count(row)
        for row in reservations
    )
    allocation.version += 1
    _recount_window(session, allocation.dispatch_claim_window_id)


def _recount_window(session: Session, window_id: str) -> None:
    window = session.get(DispatchClaimWindow, window_id)
    if window is None:
        raise RuntimeError("release_fact_incomplete")
    allocations = session.scalars(select(
        DispatchClaimShardAllocation
    ).where(
        DispatchClaimShardAllocation.dispatch_claim_window_id == window_id
    ))
    window.unclaimed_allocated_count = sum(
        row.unclaimed_allocated_count for row in allocations
    )
    window.version += 1


def _reservation_unclaimed_count(
    reservation: DispatchClaimReservation,
) -> int:
    unclaimed = (
        int(reservation.reserved_claims)
        - int(reservation.claimed_count)
        - int(reservation.released_count)
    )
    if unclaimed < int(reservation.bound_count):
        raise RuntimeError("release_fact_incomplete")
    return unclaimed


__all__ = ["reconcile_complete_release", "recount_dispatch_unclaimed"]
