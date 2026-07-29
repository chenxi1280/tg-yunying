from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    DispatchClaimReservation,
    DispatchClaimScope,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    SearchClickOpportunityAssignment,
)

from .dispatch_claim_ledger import for_update


def locked_release_assignment(
    session: Session,
    assignment_id: str,
) -> SearchClickOpportunityAssignment | None:
    hint = session.get(SearchClickOpportunityAssignment, assignment_id)
    if hint is None:
        return None
    reservation_hint = session.get(
        DispatchClaimReservation,
        hint.dispatch_claim_reservation_id,
    )
    allocation_hint = session.get(
        DispatchClaimShardAllocation,
        reservation_hint.dispatch_claim_shard_allocation_id,
    ) if reservation_hint else None
    window_hint = session.get(
        DispatchClaimWindow,
        allocation_hint.dispatch_claim_window_id,
    ) if allocation_hint else None
    if not reservation_hint or not allocation_hint or not window_hint:
        raise RuntimeError("release_fact_incomplete")
    scope = locked_release_scope(session, window_hint.dispatcher_scope)
    window = locked_release_row(session, DispatchClaimWindow, window_hint.id)
    allocation = locked_release_row(
        session,
        DispatchClaimShardAllocation,
        allocation_hint.id,
    )
    reservation = locked_release_row(
        session,
        DispatchClaimReservation,
        reservation_hint.id,
    )
    assignment = locked_assignment(session, assignment_id)
    rows = (scope, window, allocation, reservation, assignment)
    if any(row is None for row in rows):
        raise RuntimeError("release_fact_incomplete")
    if not release_rows_match((assignment, reservation, allocation, window)):
        raise RuntimeError("release_fact_incomplete")
    return assignment


def locked_release_scope(
    session: Session,
    scope_name: str,
) -> DispatchClaimScope | None:
    statement = select(DispatchClaimScope).where(
        DispatchClaimScope.dispatcher_scope == scope_name,
    )
    return session.scalar(for_update(
        session,
        statement.execution_options(populate_existing=True),
    ))


def locked_release_row(session: Session, model, row_id: str):
    statement = select(model).where(model.id == row_id)
    return session.scalar(for_update(
        session,
        statement.execution_options(populate_existing=True),
    ))


def locked_assignment(
    session: Session,
    assignment_id: str,
) -> SearchClickOpportunityAssignment | None:
    statement = select(SearchClickOpportunityAssignment).where(
        SearchClickOpportunityAssignment.id == assignment_id
    )
    return session.scalar(for_update(
        session,
        statement.execution_options(populate_existing=True),
    ))


def release_rows_match(
    rows: tuple[
        SearchClickOpportunityAssignment,
        DispatchClaimReservation,
        DispatchClaimShardAllocation,
        DispatchClaimWindow,
    ],
) -> bool:
    assignment, reservation, allocation, window = rows
    return bool(
        assignment.dispatch_claim_reservation_id == reservation.id
        and reservation.dispatch_claim_shard_allocation_id == allocation.id
        and allocation.dispatch_claim_window_id == window.id
    )


__all__ = [
    "locked_assignment",
    "locked_release_assignment",
    "locked_release_row",
    "locked_release_scope",
    "release_rows_match",
]
