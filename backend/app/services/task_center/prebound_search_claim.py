from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    DispatchAllocationExclusion,
    DispatchClaimReservation,
    DispatchClaimScope,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    SearchClickOpportunityAssignment,
)
from app.services._common import _now

from .datetime_compat import is_before
from .dispatch_claim_ledger import binding_metadata, for_update
from .dispatch_claim_types import DispatchClaimBinding, DispatchClaimPlan


def plan_prebound_search_claims(
    session: Session,
    actions: list[Action],
) -> DispatchClaimPlan:
    bindings: dict[str, DispatchClaimBinding] = {}
    for action in actions:
        binding = _prebound_binding(session, action)
        if binding is not None:
            bindings[action.id] = binding
    return DispatchClaimPlan(tuple(bindings), bindings)


def confirm_prebound_search_claim(
    session: Session,
    action: Action,
    binding: DispatchClaimBinding,
) -> bool:
    scope = session.scalar(for_update(
        session,
        select(DispatchClaimScope).where(
            DispatchClaimScope.dispatcher_scope == binding.dispatcher_scope
        ),
    ))
    window = _locked(session, DispatchClaimWindow, binding.window_id)
    allocation = _locked(
        session,
        DispatchClaimShardAllocation,
        binding.shard_allocation_id,
    )
    reservation = _locked(session, DispatchClaimReservation, binding.reservation_id)
    assignment = _locked_assignment(session, action)
    rows = (assignment, reservation, allocation, window, scope)
    if any(row is None for row in rows):
        return False
    if _unit_exclusion(session, assignment):
        return False
    if not _prebound_claim_available(
        action,
        assignment=assignment,
        reservation=reservation,
        window=window,
    ):
        return False
    reservation.bound_count -= 1
    reservation.claimed_count += 1
    reservation.version += 1
    assignment.state = "claimed"
    assignment.version += 1
    allocation.unclaimed_allocated_count -= 1
    allocation.active_claim_count += 1
    allocation.version += 1
    window.unclaimed_allocated_count -= 1
    window.active_claim_count += 1
    window.version += 1
    scope.active_claim_count += 1
    scope.version += 1
    action.result = {
        **(action.result or {}),
        **binding_metadata(binding),
        "dispatch_claim_active": True,
    }
    return True


def _prebound_binding(
    session: Session,
    action: Action,
) -> DispatchClaimBinding | None:
    result = action.result if isinstance(action.result, dict) else {}
    if not result.get("dispatch_prebound"):
        return None
    assignment = _assignment(session, action)
    if assignment is None or assignment.state != "action_bound":
        return None
    reservation = session.get(
        DispatchClaimReservation,
        assignment.dispatch_claim_reservation_id,
    )
    allocation = (
        session.get(
            DispatchClaimShardAllocation,
            reservation.dispatch_claim_shard_allocation_id,
        )
        if reservation
        else None
    )
    window = (
        session.get(DispatchClaimWindow, allocation.dispatch_claim_window_id)
        if allocation
        else None
    )
    if not _binding_rows_valid(
        action,
        assignment=assignment,
        reservation=reservation,
        window=window,
    ):
        return None
    return DispatchClaimBinding(
        reservation_id=reservation.id,
        window_id=window.id,
        shard_allocation_id=allocation.id,
        dispatcher_scope=window.dispatcher_scope,
        shard_total=allocation.account_shard_total,
        shard_index=allocation.account_shard_index,
        allocation_epoch=reservation.dispatch_allocation_epoch,
        claim_class=reservation.claim_class,
        reservation_reason=reservation.reason,
        urgency_score=reservation.urgency_score,
        unserved_strict_classes=(),
    )


def _binding_rows_valid(
    action: Action,
    *,
    assignment: SearchClickOpportunityAssignment,
    reservation: DispatchClaimReservation | None,
    window: DispatchClaimWindow | None,
) -> bool:
    if reservation is None or window is None:
        return False
    return action.status == "pending" and assignment.action_id == action.id


def _unit_exclusion(
    session: Session,
    assignment: SearchClickOpportunityAssignment,
):
    return session.scalar(select(DispatchAllocationExclusion.id).where(
        DispatchAllocationExclusion.dispatch_claim_reservation_id
        == assignment.dispatch_claim_reservation_id,
        DispatchAllocationExclusion.fulfillment_lane_claim_ordinal
        == assignment.fulfillment_lane_claim_ordinal,
    ))


def _assignment(
    session: Session,
    action: Action,
) -> SearchClickOpportunityAssignment | None:
    assignment_id = str((action.result or {}).get("search_click_assignment_id") or "")
    return session.get(SearchClickOpportunityAssignment, assignment_id)


def _locked_assignment(
    session: Session,
    action: Action,
) -> SearchClickOpportunityAssignment | None:
    assignment_id = str((action.result or {}).get("search_click_assignment_id") or "")
    return session.scalar(for_update(
        session,
        select(SearchClickOpportunityAssignment).where(
            SearchClickOpportunityAssignment.id == assignment_id
        ),
    ))


def _locked(session: Session, model, row_id: str):
    return session.scalar(for_update(
        session,
        select(model).where(model.id == row_id),
    ))


def _prebound_claim_available(
    action: Action,
    *,
    assignment: SearchClickOpportunityAssignment,
    reservation: DispatchClaimReservation,
    window: DispatchClaimWindow,
) -> bool:
    return bool(
        action.status == "claiming"
        and assignment.action_id == action.id
        and assignment.dispatch_claim_reservation_id == reservation.id
        and assignment.state == "action_bound"
        and reservation.bound_count > 0
        and _window_open(window)
    )


def _window_open(window: DispatchClaimWindow) -> bool:
    return is_before(_now(), window.bucket_end)


__all__ = ["confirm_prebound_search_claim", "plan_prebound_search_claims"]
