from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ConsistencyQuarantine,
    DispatchAllocationExclusion,
    DispatchClaimReservation,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    ExecutionAttempt,
    SearchClickAssignmentEpoch,
    SearchClickOpportunityAssignment,
)
from app.services._common import _now

from .search_click_epoch_ownership import units_from_solver_snapshot
from .search_click_release_reconciler import (
    recount_dispatch_unclaimed,
    recount_epoch_release_facts,
)

ReleaseResult = TypeVar("ReleaseResult")
REPAIRABLE_EPOCH_RELEASE_ERRORS = frozenset({
    "dispatch_release_counter_invariant",
    "dispatch_release_window_unclaimed_negative",
})


def write_release_quarantine(
    bind, *, assignment_id: str, reason_code: str, trigger_key: str,
) -> bool:
    with Session(bind) as session:
        assignment = session.get(SearchClickOpportunityAssignment, assignment_id)
        if assignment is None:
            return False
        reservation, exclusion, action, gateway_started = _related_facts(
            session, assignment
        )
        observed = _observed_state(
            assignment, reservation, exclusion, action, gateway_started)
        scope_id = (
            f"{assignment.dispatch_claim_reservation_id}:"
            f"{assignment.fulfillment_lane_claim_ordinal}"
        )
        fingerprint = _fingerprint(scope_id, reason_code, observed)
        existing = session.scalar(select(ConsistencyQuarantine).where(
            ConsistencyQuarantine.scope_type == "dispatch_reservation_unit",
            ConsistencyQuarantine.scope_id == scope_id,
            ConsistencyQuarantine.issue_fingerprint == fingerprint,
        ))
        quarantine = existing
        if quarantine is None:
            quarantine = ConsistencyQuarantine(
                tenant_id=assignment.tenant_id,
                scope_type="dispatch_reservation_unit",
                scope_id=scope_id,
                reason_code=reason_code,
                issue_fingerprint=fingerprint,
                observed_state=json.dumps(observed, sort_keys=True),
                trigger=trigger_key,
            )
            session.add(quarantine)
            session.flush()
        repaired = _repair_pre_gateway_counter(
            session,
            quarantine,
            assignment,
            reservation,
            exclusion,
            gateway_started=gateway_started,
        )
        session.commit()
        return repaired


def run_epoch_release_with_quarantine(
    session: Session,
    *,
    epoch: SearchClickAssignmentEpoch,
    trigger_key: str,
    operation: Callable[[SearchClickAssignmentEpoch], ReleaseResult],
) -> ReleaseResult:
    epoch_id = epoch.id
    try:
        return operation(epoch)
    except RuntimeError as exc:
        if str(exc) not in REPAIRABLE_EPOCH_RELEASE_ERRORS:
            raise
        bind = session.get_bind()
        session.rollback()
        repaired = write_epoch_release_quarantine(
            bind,
            epoch_id=epoch_id,
            reason_code=str(exc),
            trigger_key=trigger_key,
        )
        if not repaired:
            raise
        repaired_epoch = session.get(SearchClickAssignmentEpoch, epoch_id)
        if repaired_epoch is None:
            raise RuntimeError("search_click_assignment_epoch_missing")
        return operation(repaired_epoch)


def write_epoch_release_quarantine(
    bind,
    *,
    epoch_id: str,
    reason_code: str,
    trigger_key: str,
) -> bool:
    with Session(bind) as session:
        epoch = session.get(SearchClickAssignmentEpoch, epoch_id)
        if epoch is None:
            return False
        units = list(units_from_solver_snapshot(session, epoch))
        reservation_ids = sorted({unit.reservation_id for unit in units})
        reservations = _epoch_reservations(session, reservation_ids)
        if not units or len(reservations) != len(reservation_ids):
            return False
        observed = _epoch_observed_state(session, epoch, units, reservations)
        quarantine = _get_or_add_epoch_quarantine(
            session,
            epoch=epoch,
            tenant_id=reservations[0].tenant_id,
            reason_code=reason_code,
            trigger_key=trigger_key,
            observed=observed,
        )
        try:
            with session.begin_nested():
                recount_epoch_release_facts(
                    session,
                    window_id=epoch.dispatch_claim_window_id,
                    reservation_ids=reservation_ids,
                )
        except RuntimeError:
            session.commit()
            return False
        quarantine.status = "resolved"
        quarantine.resolved_at = _now()
        session.commit()
        return True


def _epoch_reservations(
    session: Session,
    reservation_ids: list[str],
) -> list[DispatchClaimReservation]:
    return list(session.scalars(
        select(DispatchClaimReservation)
        .where(DispatchClaimReservation.id.in_(reservation_ids))
        .order_by(DispatchClaimReservation.id)
    ))


def _epoch_observed_state(session, epoch, units, reservations) -> dict:
    allocations = {
        row.id: row
        for row in session.scalars(select(DispatchClaimShardAllocation).where(
            DispatchClaimShardAllocation.id.in_({
                item.dispatch_claim_shard_allocation_id for item in reservations
            })
        ))
    }
    window = session.get(DispatchClaimWindow, epoch.dispatch_claim_window_id)
    return {
        "epoch_id": epoch.id,
        "window": {
            "id": window.id if window else None,
            "unclaimed": window.unclaimed_allocated_count if window else None,
            "version": window.version if window else None,
        },
        "units": [
            {"reservation_id": unit.reservation_id,
             "ordinal": unit.fulfillment_lane_claim_ordinal}
            for unit in units
        ],
        "reservations": [_reservation_observed(row, allocations) for row in reservations],
    }


def _reservation_observed(reservation, allocations) -> dict:
    allocation = allocations.get(reservation.dispatch_claim_shard_allocation_id)
    return {
        "id": reservation.id,
        "reserved": reservation.reserved_claims,
        "bound": reservation.bound_count,
        "claimed": reservation.claimed_count,
        "released": reservation.released_count,
        "allocation_id": allocation.id if allocation else None,
        "allocation_unclaimed": allocation.unclaimed_allocated_count if allocation else None,
    }


def _get_or_add_epoch_quarantine(
    session: Session,
    *,
    epoch: SearchClickAssignmentEpoch,
    tenant_id: int,
    reason_code: str,
    trigger_key: str,
    observed: dict,
) -> ConsistencyQuarantine:
    fingerprint = _fingerprint(epoch.id, reason_code, observed)
    existing = session.scalar(select(ConsistencyQuarantine).where(
        ConsistencyQuarantine.scope_type == "search_click_assignment_epoch",
        ConsistencyQuarantine.scope_id == epoch.id,
        ConsistencyQuarantine.issue_fingerprint == fingerprint,
    ))
    if existing is not None:
        return existing
    quarantine = ConsistencyQuarantine(
        tenant_id=tenant_id,
        scope_type="search_click_assignment_epoch",
        scope_id=epoch.id,
        reason_code=reason_code,
        issue_fingerprint=fingerprint,
        observed_state=json.dumps(observed, sort_keys=True),
        trigger=trigger_key,
    )
    session.add(quarantine)
    session.flush()
    return quarantine


def _related_facts(session: Session, assignment):
    reservation = session.get(
        DispatchClaimReservation,
        assignment.dispatch_claim_reservation_id,
    )
    exclusion = session.scalar(select(DispatchAllocationExclusion).where(
        DispatchAllocationExclusion.dispatch_claim_reservation_id
        == assignment.dispatch_claim_reservation_id,
        DispatchAllocationExclusion.fulfillment_lane_claim_ordinal
        == assignment.fulfillment_lane_claim_ordinal,
    ))
    action = session.get(Action, assignment.action_id) if assignment.action_id else None
    gateway_started = bool(action and session.scalar(
        select(ExecutionAttempt.id).where(
            ExecutionAttempt.action_id == action.id,
            ExecutionAttempt.gateway_call_started_at.is_not(None),
        ).limit(1)
    ))
    return reservation, exclusion, action, gateway_started


def _repair_pre_gateway_counter(
    session: Session,
    quarantine: ConsistencyQuarantine,
    assignment: SearchClickOpportunityAssignment,
    reservation: DispatchClaimReservation | None,
    exclusion: DispatchAllocationExclusion | None,
    *,
    gateway_started: bool,
) -> bool:
    if (
        reservation is None
        or exclusion is not None
        or gateway_started
        or assignment.state not in {"reserved", "action_bound"}
    ):
        return False
    bound = int(session.scalar(
        select(func.count(SearchClickOpportunityAssignment.id)).where(
            SearchClickOpportunityAssignment.dispatch_claim_reservation_id
            == reservation.id,
            SearchClickOpportunityAssignment.state.in_(
                ("reserved", "action_bound")
            ),
        )
    ) or 0)
    occupied = bound + reservation.claimed_count + reservation.released_count
    if occupied > reservation.reserved_claims:
        return False
    reservation.bound_count = bound
    reservation.version += 1
    recount_dispatch_unclaimed(session, reservation)
    quarantine.status = "resolved"
    quarantine.resolved_at = _now()
    return True


def _observed_state(
    assignment,
    reservation,
    exclusion,
    action,
    gateway_started: bool,
) -> dict:
    return {
        "assignment": {
            "id": assignment.id,
            "state": assignment.state,
            "version": assignment.version,
            "action_id": assignment.action_id,
        },
        "reservation": {
            "id": reservation.id if reservation else None,
            "version": reservation.version if reservation else None,
            "reserved": reservation.reserved_claims if reservation else None,
            "bound": reservation.bound_count if reservation else None,
            "claimed": reservation.claimed_count if reservation else None,
            "released": reservation.released_count if reservation else None,
        },
        "exclusion": {
            "id": exclusion.id if exclusion else None,
            "carrier_type": exclusion.carrier_type if exclusion else None,
            "carrier_id": exclusion.carrier_id if exclusion else None,
            "state": exclusion.state if exclusion else None,
        },
        "action": {
            "id": action.id if action else None,
            "status": action.status if action else None,
            "gateway_started": gateway_started,
        },
    }


def _fingerprint(scope_id: str, reason_code: str, observed: dict) -> str:
    payload = json.dumps(
        [scope_id, reason_code, observed],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "run_epoch_release_with_quarantine",
    "write_epoch_release_quarantine",
    "write_release_quarantine",
]
