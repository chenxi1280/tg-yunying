from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.models import (
    DispatchClaimWindow,
    SearchClickAssignmentEpoch,
    SearchClickOpportunityAssignment,
)


def release_unit_set_hash(release_facts: list[dict]) -> str:
    values = sorted(
        release_facts,
        key=lambda item: (item["reservation_id"], item["ordinal"]),
    )
    return _hash(values)


def search_path_snapshot_hash(candidate: object) -> str:
    return hashlib.sha256(repr(candidate).encode()).hexdigest()


def finalize_search_outcome_hash(
    session: Session,
    epoch: SearchClickAssignmentEpoch,
    *,
    solver_result: dict,
) -> str:
    assignments = tuple(session.scalars(
        select(SearchClickOpportunityAssignment)
        .where(
            SearchClickOpportunityAssignment.search_click_assignment_epoch_id
            == epoch.id
        )
        .order_by(SearchClickOpportunityAssignment.id)
    ))
    return search_outcome_hash(
        epoch,
        solver_result=solver_result,
        matches=assignments,
    )


def search_outcome_hash(
    epoch: SearchClickAssignmentEpoch,
    *,
    solver_result: dict,
    matches,
) -> str:
    payload = {
        "carrier": {
            "window_id": epoch.dispatch_claim_window_id,
            "dispatch_allocation_epoch": epoch.dispatch_allocation_epoch,
            "search_click_assignment_epoch": epoch.id,
        },
        "solver_problem_hash": epoch.solver_problem_hash,
        "solver_input_hash": epoch.solver_input_hash,
        "solver_result": solver_result,
        "matches": [_assignment_identity(row) for row in matches],
        "release_unit_set_hash": epoch.release_unit_set_hash,
        "next_dispatch_allocation_epoch": _next_dispatch_epoch(epoch),
        "rebuild_input_version_after": epoch.rebuild_input_version_after,
    }
    return _hash(payload)


def _assignment_identity(row: SearchClickOpportunityAssignment) -> dict:
    return {
        "assignment_id": row.id,
        "obligation_id": row.obligation_id,
        "reservation_id": row.dispatch_claim_reservation_id,
        "ordinal": row.fulfillment_lane_claim_ordinal,
        "resource_snapshot_hash": row.resource_snapshot_hash,
        "version": row.version,
    }


def _next_dispatch_epoch(epoch: SearchClickAssignmentEpoch) -> int | None:
    session = object_session(epoch)
    if session is None:
        return None
    window = session.get(DispatchClaimWindow, epoch.dispatch_claim_window_id)
    if window is None or epoch.rebuild_input_version_after is None:
        return None
    return int(window.allocation_epoch)


def _hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "finalize_search_outcome_hash",
    "release_unit_set_hash",
    "search_outcome_hash",
    "search_path_snapshot_hash",
]
