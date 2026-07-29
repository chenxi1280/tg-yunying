from __future__ import annotations

import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ConsistencyQuarantine,
    DispatchAllocationExclusion,
    DispatchClaimReservation,
    ExecutionAttempt,
    SearchClickOpportunityAssignment,
)
from app.services._common import _now

from .search_click_release_reconciler import recount_dispatch_unclaimed


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


__all__ = ["write_release_quarantine"]
