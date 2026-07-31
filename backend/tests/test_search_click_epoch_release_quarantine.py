from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ConsistencyQuarantine,
    DispatchAllocationExclusion,
    DispatchClaimReservation,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    SearchClickAssignmentEpoch,
    SearchClickOpportunityAssignment,
    SearchClickSolverCarrierUnitBinding,
    SearchClickSolverProblemSnapshot,
    Task,
)
from app.services.task_center.executors import search_click
from search_click_assignment_test_support import seed_assignment

pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize(
    ("drift_target", "reason_code"),
    (
        ("window", "dispatch_release_window_unclaimed_negative"),
        ("shard", "dispatch_release_counter_invariant"),
    ),
)
def test_orphan_epoch_repairs_counter_before_release(
    monkeypatch,
    drift_target: str,
    reason_code: str,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        seed_assignment(session)
        _prepare_orphaned_epoch_with_counter_drift(session, drift_target)
        monkeypatch.setattr(
            search_click,
            "prepare_search_click_fulfillment_units",
            lambda *_args, **_kwargs: (),
        )

        assert search_click.build_plan(session, session.get(Task, "task-1")) == 0
        session.commit()

        epoch = session.get(SearchClickAssignmentEpoch, "epoch-1")
        reservation = session.get(DispatchClaimReservation, "reservation-1")
        allocation = session.get(DispatchClaimShardAllocation, "shard-1")
        window = session.get(DispatchClaimWindow, "window-1")
        quarantine = session.scalar(select(ConsistencyQuarantine))
        exclusion = session.scalar(select(DispatchAllocationExclusion))
        assert epoch.finalize_status == "finalized"
        assert reservation.released_count == 1
        assert allocation.unclaimed_allocated_count == 0
        assert window.unclaimed_allocated_count == 0
        assert quarantine.reason_code == reason_code
        assert quarantine.status == "resolved"
        assert exclusion.carrier_id == epoch.id


def test_orphan_epoch_with_lost_reservation_capacity_stays_quarantined(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        seed_assignment(session)
        _prepare_orphaned_epoch_with_counter_drift(session, "none")
        reservation = session.get(DispatchClaimReservation, "reservation-1")
        reservation.reserved_claims = 0
        session.commit()
        monkeypatch.setattr(
            search_click,
            "prepare_search_click_fulfillment_units",
            lambda *_args, **_kwargs: (),
        )

        assert search_click.build_plan(session, session.get(Task, "task-1")) == 0
        session.commit()
        assert search_click.build_plan(session, session.get(Task, "task-1")) == 0

        epoch = session.get(SearchClickAssignmentEpoch, "epoch-1")
        quarantine = session.scalar(select(ConsistencyQuarantine).where(
            ConsistencyQuarantine.status == "active"
        ))
        assert epoch.finalize_status == "open"
        assert quarantine is not None
        assert quarantine.reason_code == "search_reservation_ownership_violation"
        assert session.scalar(select(DispatchAllocationExclusion)) is None


def _prepare_orphaned_epoch_with_counter_drift(
    session: Session,
    drift_target: str,
) -> None:
    assignment = session.get(SearchClickOpportunityAssignment, "assignment-1")
    action = session.get(Action, "action-1")
    session.delete(assignment)
    session.delete(action)
    reservation = session.get(DispatchClaimReservation, "reservation-1")
    reservation.bound_count = 0
    window = session.get(DispatchClaimWindow, "window-1")
    allocation = session.get(DispatchClaimShardAllocation, "shard-1")
    if drift_target == "window":
        window.unclaimed_allocated_count = 0
    elif drift_target == "shard":
        allocation.unclaimed_allocated_count = 0
    epoch = session.get(SearchClickAssignmentEpoch, "epoch-1")
    epoch.outcome = "open"
    epoch.finalize_status = "open"
    snapshot = SearchClickSolverProblemSnapshot(
        id="snapshot-1",
        search_click_assignment_epoch_id=epoch.id,
        solver_contract_version="v1",
        canonical_problem_payload={},
        canonical_carrier_payload={},
        solver_problem_hash=epoch.solver_problem_hash,
        solver_input_hash=epoch.solver_input_hash,
    )
    session.add(snapshot)
    session.add(SearchClickSolverCarrierUnitBinding(
        search_click_solver_snapshot_id=snapshot.id,
        dispatch_claim_reservation_id=reservation.id,
        fulfillment_lane_claim_ordinal=1,
        obligation_id="obligation-1",
        task_id="task-1",
        stable_component_key="component-1",
        solver_problem_component_hash="c" * 64,
        canonical_binding_payload={},
    ))
    session.commit()
