from __future__ import annotations

from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    SearchClickAssignmentEpoch,
    SearchClickSolverCarrierUnitBinding,
    SearchClickSolverProblemSnapshot,
    WorkerHeartbeat,
)

from .heartbeat import worker_identity
from app.services._common import _now
from .search_click_dispatch_allocation import SearchClickFulfillmentUnit

SOLVER_OWNER_STALE_SECONDS = 120


def solver_owner_is_active(
    session: Session,
    epoch: SearchClickAssignmentEpoch,
) -> bool:
    heartbeat = session.get(WorkerHeartbeat, epoch.solver_owner_lease_id)
    if heartbeat is None or heartbeat.status != "active":
        return False
    metadata = heartbeat.heartbeat_metadata or {}
    token_matches = (
        metadata.get("search_solver_fencing_token") == epoch.solver_fencing_token
    )
    current_worker_id, current_hostname, current_pid = worker_identity(
        "search_click_solver"
    )
    restarted_owner = (
        heartbeat.worker_id == current_worker_id
        and heartbeat.hostname == current_hostname
        and heartbeat.pid != current_pid
    )
    last_seen = heartbeat.last_seen_at
    current = _now()
    if last_seen.tzinfo:
        last_seen = last_seen.replace(tzinfo=None)
    if current.tzinfo:
        current = current.replace(tzinfo=None)
    owner_stale = last_seen < current - timedelta(
        seconds=SOLVER_OWNER_STALE_SECONDS
    )
    return token_matches and not restarted_owner and not owner_stale


def units_from_solver_snapshot(
    session: Session,
    epoch: SearchClickAssignmentEpoch,
) -> tuple[SearchClickFulfillmentUnit, ...]:
    snapshot = session.scalar(select(SearchClickSolverProblemSnapshot).where(
        SearchClickSolverProblemSnapshot.search_click_assignment_epoch_id == epoch.id
    ))
    if snapshot is None:
        return ()
    bindings = list(session.scalars(
        select(SearchClickSolverCarrierUnitBinding)
        .where(
            SearchClickSolverCarrierUnitBinding.search_click_solver_snapshot_id
            == snapshot.id
        )
        .order_by(
            SearchClickSolverCarrierUnitBinding.dispatch_claim_reservation_id,
            SearchClickSolverCarrierUnitBinding.fulfillment_lane_claim_ordinal,
        )
    ))
    return tuple(
        SearchClickFulfillmentUnit(
            item.obligation_id,
            item.task_id,
            item.dispatch_claim_reservation_id,
            epoch.dispatch_claim_window_id,
            epoch.dispatch_allocation_epoch,
            item.fulfillment_lane_claim_ordinal,
        )
        for item in bindings
    )


__all__ = ["solver_owner_is_active", "units_from_solver_snapshot"]
