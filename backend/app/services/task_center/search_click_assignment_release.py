from __future__ import annotations

from datetime import datetime
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.models import (
    Action,
    DispatchAllocationExclusion,
    DispatchAllocationReleaseBatch,
    DispatchAllocationReleaseBatchItem,
    DispatchClaimReservation,
    DispatchClaimScope,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    ExecutionAttempt,
    SearchClickFulfillmentObligation,
    SearchClickOpportunityAssignment,
)

from .dispatch_release_wave import start_or_join_dispatch_rebuild_wave
from .search_click_release_locking import locked_release_assignment
from .search_click_release_reconciler import reconcile_complete_release
from .search_click_release_quarantine import write_release_quarantine

ALLOWED_RELEASE_REASONS = {
    "no_feasible_search_path",
    "search_resource_saturated",
    "protocol_ineligible_for_snapshot",
    "search_solver_abandoned",
    "search_reservation_cas_abandoned",
    "search_assignment_pre_gateway_terminal",
    "search_assignment_expired",
    "unclaimed_action_no_longer_due",
}


def release_search_click_assignment(
    session: Session,
    assignment_id: str,
    *,
    trigger_key: str,
    reason_code: str,
    now_value: datetime,
) -> DispatchAllocationReleaseBatch:
    try:
        return _release_search_click_assignment(
            session,
            assignment_id,
            trigger_key=trigger_key,
            reason_code=reason_code,
            now_value=now_value,
        )
    except RuntimeError as exc:
        if str(exc) not in {
            "release_fact_incomplete",
            "release_claim_fact_conflict",
            "dispatch_release_counter_invariant",
            "dispatch_release_active_counter_invariant",
        }:
            raise
        bind = session.get_bind()
        session.rollback()
        repaired = write_release_quarantine(
            bind,
            assignment_id=assignment_id,
            reason_code=str(exc),
            trigger_key=trigger_key,
        )
        if repaired:
            return _release_search_click_assignment(
                session,
                assignment_id,
                trigger_key=trigger_key,
                reason_code=reason_code,
                now_value=now_value,
            )
        raise


def _release_search_click_assignment(
    session: Session, assignment_id: str, *, trigger_key: str,
    reason_code: str, now_value: datetime,
) -> DispatchAllocationReleaseBatch:
    if reason_code not in ALLOWED_RELEASE_REASONS:
        raise ValueError("search_click_release_reason_invalid")
    assignment = locked_release_assignment(session, assignment_id)
    if assignment is None:
        raise ValueError("search_click_assignment_not_found")
    candidate_hash = _candidate_hash(assignment)
    existing = _existing_batch(session, assignment, trigger_key)
    if existing is not None:
        if existing.candidate_unit_set_hash != candidate_hash:
            raise RuntimeError("release_fact_incomplete")
        _validate_finalized_batch(session, existing)
        return existing
    batch = _new_batch(assignment, trigger_key, candidate_hash)
    session.add(batch)
    session.flush()
    classification, carrier = _classify_release(
        session,
        assignment,
        now_value=now_value,
    )
    _add_batch_item(session, batch, assignment=assignment,
                    classification=classification, carrier=carrier)
    if classification == "released":
        _apply_release(session, batch, assignment=assignment,
                       reason_code=reason_code, now_value=now_value)
    session.flush()
    batch.release_unit_set_hash = _release_hash(
        session,
        assignment,
        classification=classification,
        reason_code=reason_code,
    )
    _finalize_batch(batch, assignment=assignment,
                    classification=classification, now_value=now_value)
    return batch


def _existing_batch(
    session: Session,
    assignment: SearchClickOpportunityAssignment,
    trigger_key: str,
) -> DispatchAllocationReleaseBatch | None:
    reservation = session.get(
        DispatchClaimReservation,
        assignment.dispatch_claim_reservation_id,
    )
    allocation = session.get(
        DispatchClaimShardAllocation,
        reservation.dispatch_claim_shard_allocation_id,
    )
    return session.scalar(select(DispatchAllocationReleaseBatch).where(
        DispatchAllocationReleaseBatch.dispatch_claim_window_id
        == allocation.dispatch_claim_window_id,
        DispatchAllocationReleaseBatch.trigger_key == trigger_key,
    ))


def _new_batch(
    assignment: SearchClickOpportunityAssignment,
    trigger_key: str,
    candidate_hash: str,
) -> DispatchAllocationReleaseBatch:
    reservation = assignment.dispatch_claim_reservation_id
    session = object_session(assignment)
    if session is None:
        raise RuntimeError("search_click_assignment_session_missing")
    reservation_row = session.get(DispatchClaimReservation, reservation)
    allocation = session.get(
        DispatchClaimShardAllocation,
        reservation_row.dispatch_claim_shard_allocation_id,
    )
    return DispatchAllocationReleaseBatch(
        dispatch_claim_window_id=allocation.dispatch_claim_window_id,
        dispatch_allocation_epoch=reservation_row.dispatch_allocation_epoch,
        trigger_key=trigger_key,
        candidate_unit_set_hash=candidate_hash,
        candidate_unit_count=1,
    )


def _classify_release(
    session: Session,
    assignment: SearchClickOpportunityAssignment,
    *,
    now_value: datetime,
) -> tuple[str, tuple[str, str]]:
    exclusion = session.scalar(select(DispatchAllocationExclusion).where(
        DispatchAllocationExclusion.dispatch_claim_reservation_id
        == assignment.dispatch_claim_reservation_id,
        DispatchAllocationExclusion.fulfillment_lane_claim_ordinal
        == assignment.fulfillment_lane_claim_ordinal,
    ))
    if exclusion is not None:
        carrier = reconcile_complete_release(
            session,
            assignment,
            exclusion=exclusion,
            now_value=now_value,
        )
        return "already_released", carrier
    if assignment.state == "released":
        raise RuntimeError("release_fact_incomplete")
    action = session.get(Action, assignment.action_id) if assignment.action_id else None
    gateway_started = bool(action and session.scalar(
        select(ExecutionAttempt.id).where(
            ExecutionAttempt.action_id == action.id,
            ExecutionAttempt.gateway_call_started_at.is_not(None),
        ).limit(1)
    ))
    releasable = (
        assignment.state in {"reserved", "action_bound", "claimed"}
        and (
            action is None
            or action.status
            in {"pending", "claiming", "executing", "skipped", "failed"}
        )
        and not gateway_started
    )
    return ("released", ("", "")) if releasable else ("precondition_lost", ("", ""))


def _add_batch_item(
    session: Session,
    batch: DispatchAllocationReleaseBatch,
    *,
    assignment: SearchClickOpportunityAssignment,
    classification: str,
    carrier: tuple[str, str],
) -> None:
    action = session.get(Action, assignment.action_id) if assignment.action_id else None
    action_version = int(action.retry_count) if action else None
    session.add(DispatchAllocationReleaseBatchItem(
        release_batch_id=batch.id,
        assignment_id=assignment.id,
        dispatch_claim_reservation_id=assignment.dispatch_claim_reservation_id,
        fulfillment_lane_claim_ordinal=assignment.fulfillment_lane_claim_ordinal,
        expected_assignment_version=assignment.version,
        observed_assignment_version=assignment.version,
        expected_action_version=action_version,
        observed_action_version=action_version,
        classification=classification,
        first_carrier_type=carrier[0],
        first_carrier_id=carrier[1],
    ))


def _apply_release(
    session: Session,
    batch: DispatchAllocationReleaseBatch,
    *,
    assignment: SearchClickOpportunityAssignment,
    reason_code: str,
    now_value: datetime,
) -> None:
    reservation = session.get(
        DispatchClaimReservation,
        assignment.dispatch_claim_reservation_id,
    )
    allocation = session.get(
        DispatchClaimShardAllocation,
        reservation.dispatch_claim_shard_allocation_id,
    )
    was_claimed = assignment.state == "claimed"
    _release_reservation_counter(reservation, was_claimed)
    if not was_claimed and allocation.unclaimed_allocated_count <= 0:
        raise RuntimeError("dispatch_release_counter_invariant")
    if was_claimed:
        _release_active_counter(session, batch, allocation)
    else:
        allocation.unclaimed_allocated_count -= 1
    reservation.released_count += 1
    reservation.version += 1
    allocation.version += 1
    assignment.state = "released"
    assignment.release_reason = reason_code
    assignment.version += 1
    _close_released_action(
        session,
        assignment,
        reason_code=reason_code,
        now_value=now_value,
    )
    _add_release_exclusion(
        session,
        batch,
        assignment,
        reason_code=reason_code,
    )
    batch.rebuild_input_version_after = start_or_join_dispatch_rebuild_wave(
        session,
        window_id=batch.dispatch_claim_window_id,
        released_count=1,
        now_value=now_value,
        decrement_unclaimed=not was_claimed,
    )


def _add_release_exclusion(
    session: Session,
    batch: DispatchAllocationReleaseBatch,
    assignment: SearchClickOpportunityAssignment,
    *,
    reason_code: str,
) -> None:
    session.add(DispatchAllocationExclusion(
        dispatch_claim_window_id=batch.dispatch_claim_window_id,
        dispatch_claim_reservation_id=assignment.dispatch_claim_reservation_id,
        fulfillment_lane_claim_ordinal=assignment.fulfillment_lane_claim_ordinal,
        carrier_type="dispatch_allocation_release_batch",
        carrier_id=batch.id,
        reason_code=reason_code,
        resource_snapshot_hash=assignment.resource_snapshot_hash,
    ))


def _release_reservation_counter(
    reservation: DispatchClaimReservation,
    was_claimed: bool,
) -> None:
    field = "claimed_count" if was_claimed else "bound_count"
    value = int(getattr(reservation, field))
    if value <= 0:
        raise RuntimeError("dispatch_release_counter_invariant")
    setattr(reservation, field, value - 1)


def _release_active_counter(
    session: Session,
    batch: DispatchAllocationReleaseBatch,
    allocation: DispatchClaimShardAllocation,
) -> None:
    window = session.get(DispatchClaimWindow, batch.dispatch_claim_window_id)
    scope = session.scalar(select(DispatchClaimScope).where(
        DispatchClaimScope.dispatcher_scope == window.dispatcher_scope
    ))
    counters = (
        allocation.active_claim_count,
        window.active_claim_count,
        scope.active_claim_count if scope else 0,
    )
    if any(int(value) <= 0 for value in counters):
        raise RuntimeError("dispatch_release_active_counter_invariant")
    allocation.active_claim_count -= 1
    window.active_claim_count -= 1
    window.version += 1
    scope.active_claim_count -= 1
    scope.version += 1


def _close_released_action(
    session: Session,
    assignment: SearchClickOpportunityAssignment,
    *,
    reason_code: str,
    now_value: datetime,
) -> None:
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


def _finalize_batch(
    batch: DispatchAllocationReleaseBatch,
    *,
    assignment: SearchClickOpportunityAssignment,
    classification: str,
    now_value: datetime,
) -> None:
    batch.release_unit_count = int(classification == "released")
    batch.already_released_unit_count = int(classification == "already_released")
    batch.precondition_lost_unit_count = int(classification == "precondition_lost")
    batch.outcome_hash = _outcome_hash(batch, assignment, classification)
    batch.finalize_status = "finalized"
    batch.finalized_at = now_value


def _candidate_hash(assignment: SearchClickOpportunityAssignment) -> str:
    return _hash((
        assignment.dispatch_claim_reservation_id,
        assignment.fulfillment_lane_claim_ordinal,
    ))


def _outcome_hash(
    batch: DispatchAllocationReleaseBatch,
    assignment: SearchClickOpportunityAssignment,
    classification: str,
) -> str:
    return _hash({
        "carrier": {
            "window_id": batch.dispatch_claim_window_id,
            "dispatch_allocation_epoch": batch.dispatch_allocation_epoch,
            "release_batch_id": batch.id,
            "trigger_key": batch.trigger_key,
        },
        "candidate_hash": batch.candidate_unit_set_hash,
        "release_unit_set_hash": batch.release_unit_set_hash,
        "assignment_id": assignment.id,
        "assignment_version": assignment.version,
        "classification": classification,
        "released": batch.release_unit_count,
        "already_released": batch.already_released_unit_count,
        "precondition_lost": batch.precondition_lost_unit_count,
        "rebuild_input_version_after": batch.rebuild_input_version_after,
        "next_dispatch_allocation_epoch": _next_dispatch_epoch(batch),
    })


def _release_hash(
    session: Session,
    assignment: SearchClickOpportunityAssignment,
    *,
    classification: str,
    reason_code: str,
) -> str:
    if classification != "released":
        return _hash([])
    exclusion = session.scalar(select(DispatchAllocationExclusion).where(
        DispatchAllocationExclusion.dispatch_claim_reservation_id
        == assignment.dispatch_claim_reservation_id,
        DispatchAllocationExclusion.fulfillment_lane_claim_ordinal
        == assignment.fulfillment_lane_claim_ordinal,
    ))
    if exclusion is None:
        raise RuntimeError("release_fact_incomplete")
    return _hash([{
        "window_id": exclusion.dispatch_claim_window_id,
        "reservation_id": exclusion.dispatch_claim_reservation_id,
        "ordinal": exclusion.fulfillment_lane_claim_ordinal,
        "reason_code": reason_code,
        "resource_snapshot_hash": exclusion.resource_snapshot_hash,
    }])


def _next_dispatch_epoch(batch: DispatchAllocationReleaseBatch) -> int | None:
    session = object_session(batch)
    if session is None or batch.rebuild_input_version_after is None:
        return None
    window = session.get(DispatchClaimWindow, batch.dispatch_claim_window_id)
    return int(window.allocation_epoch) if window is not None else None


def _validate_finalized_batch(
    session: Session,
    batch: DispatchAllocationReleaseBatch,
) -> None:
    items = list(session.scalars(select(DispatchAllocationReleaseBatchItem).where(
        DispatchAllocationReleaseBatchItem.release_batch_id == batch.id
    )))
    if batch.finalize_status != "finalized" or len(items) != batch.candidate_unit_count:
        raise RuntimeError("release_fact_incomplete")
    counts = {
        "released": batch.release_unit_count,
        "already_released": batch.already_released_unit_count,
        "precondition_lost": batch.precondition_lost_unit_count,
    }
    if sum(counts.values()) != batch.candidate_unit_count:
        raise RuntimeError("release_fact_incomplete")
    observed = {
        key: sum(item.classification == key for item in items)
        for key in counts
    }
    if observed != counts or not batch.release_unit_set_hash or not batch.outcome_hash:
        raise RuntimeError("release_fact_incomplete")


def _hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["ALLOWED_RELEASE_REASONS", "release_search_click_assignment"]
