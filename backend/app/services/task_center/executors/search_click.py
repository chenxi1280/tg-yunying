from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Action,
    DispatchAllocationExclusion,
    DispatchClaimReservation,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    SearchClickAssignmentEpoch,
    SearchClickFulfillmentObligation,
    SearchClickOpportunityAssignment,
    Task,
)
from app.services._common import _now

from ..heartbeat import record_worker_heartbeat
from ..search_click_epoch_ownership import solver_owner_is_active, units_from_solver_snapshot
from ..dispatch_release_wave import start_or_join_dispatch_rebuild_wave
from ..payloads import create_search_join_action
from ..search_click_assignment_solver import SearchClickCandidatePath, solve_search_click_assignments
from ..search_click_dispatch_allocation import (
    SearchClickFulfillmentUnit,
    prepare_search_click_fulfillment_units,
)
from ..search_click_demands import record_task_opportunities, search_click_demands
from ..search_click_solver_snapshot import (
    SearchSolverSnapshot,
    assemble_search_solver_snapshot,
    persist_search_solver_snapshot,
    solver_component_hash_for_unit,
)
from ..search_click_outcome_identity import (
    finalize_search_outcome_hash,
    release_unit_set_hash,
    search_outcome_hash,
)
from ..search_click_solver_lease import SolverLeaseRenewal
from .search_click_candidates import (
    SearchClickPathContext,
    candidate_paths,
    store_capacity_projection,
)
from .search_join_group import _payload


@dataclass(frozen=True)
class _FinalizeContext:
    epoch: SearchClickAssignmentEpoch
    units: tuple[SearchClickFulfillmentUnit, ...]
    paths: dict[str, SearchClickPathContext]
    result: object
    now: datetime


@dataclass(frozen=True)
class _AssignmentContext:
    unit: SearchClickFulfillmentUnit
    path: SearchClickPathContext
    assignment: SearchClickOpportunityAssignment
    obligation: SearchClickFulfillmentObligation
    reservation: DispatchClaimReservation
    now: datetime


def build_plan(session: Session, task: Task) -> int:
    if task.type != "search_click":
        raise ValueError("search_click_executor_task_type_invalid")
    now_value = _now()
    _finalize_orphaned_epochs(session, now_value)
    units = prepare_search_click_fulfillment_units(session, now=now_value)
    if not units:
        return 0
    window_id = units[0].window_id
    allocation_epoch = units[0].dispatch_allocation_epoch
    existing = _existing_epoch(session, window_id, allocation_epoch)
    if existing is not None:
        return _handle_existing_epoch(session, existing, now_value)
    paths = candidate_paths(session, units, now_value)
    store_capacity_projection(session, units, paths)
    demands = search_click_demands(session, units)
    snapshot = assemble_search_solver_snapshot(
        session,
        units,
        demands=demands,
        paths=tuple(context.candidate for context in paths.values()),
    )
    epoch = _create_open_epoch(
        session,
        units[0],
        snapshot=snapshot,
        now_value=now_value,
    )
    if epoch is None:
        existing = _existing_epoch(session, window_id, allocation_epoch)
        return _handle_existing_epoch(session, existing, now_value)
    with SolverLeaseRenewal(
        session.get_bind(),
        epoch.solver_owner_lease_id,
        epoch.solver_fencing_token,
    ):
        result = solve_search_click_assignments(
            snapshot.demands,
            snapshot.paths,
            solver_problem_hash=snapshot.problem_hash,
            solver_input_hash=snapshot.input_hash,
        )
    created_by_task = _finalize_epoch(
        session,
        _FinalizeContext(epoch, units, paths, result, now_value),
    )
    return created_by_task.get(task.id, 0)


def _finalize_orphaned_epochs(
    session: Session,
    now_value: datetime,
) -> None:
    epochs = list(session.scalars(
        select(SearchClickAssignmentEpoch)
        .where(SearchClickAssignmentEpoch.finalize_status == "open")
        .order_by(SearchClickAssignmentEpoch.created_at)
        .with_for_update(skip_locked=True)
    ))
    for epoch in epochs:
        _handle_existing_epoch(session, epoch, now_value)


def _existing_epoch(
    session: Session,
    window_id: str,
    allocation_epoch: int,
) -> SearchClickAssignmentEpoch | None:
    return session.scalar(select(SearchClickAssignmentEpoch).where(
        SearchClickAssignmentEpoch.dispatch_claim_window_id == window_id,
        SearchClickAssignmentEpoch.dispatch_allocation_epoch == allocation_epoch,
    ))


def _create_open_epoch(
    session: Session,
    unit: SearchClickFulfillmentUnit,
    *,
    snapshot: SearchSolverSnapshot,
    now_value: datetime,
) -> SearchClickAssignmentEpoch | None:
    window = session.get(DispatchClaimWindow, unit.window_id)
    if window is None:
        raise RuntimeError("search_solver_window_missing")
    heartbeat = record_worker_heartbeat(
        session,
        process_type="search_click_solver",
        metadata={"search_solver_fencing_token": str(uuid4())},
    )
    token = str(heartbeat.heartbeat_metadata["search_solver_fencing_token"])
    epoch = SearchClickAssignmentEpoch(
        dispatch_claim_window_id=unit.window_id,
        dispatch_allocation_epoch=unit.dispatch_allocation_epoch,
        solver_owner_lease_id=heartbeat.id,
        solver_fencing_token=token,
        solver_claimed_at=now_value,
        solver_problem_hash=snapshot.problem_hash,
        solver_input_hash=snapshot.input_hash,
        rebuild_input_version_before=window.rebuild_input_version,
    )
    try:
        with session.begin_nested():
            session.add(epoch)
            session.flush()
            persist_search_solver_snapshot(session, epoch.id, snapshot)
    except IntegrityError:
        return None
    session.commit()
    return epoch


def _handle_existing_epoch(
    session: Session,
    epoch: SearchClickAssignmentEpoch | None,
    now_value: datetime,
) -> int:
    if epoch is None or epoch.finalize_status == "finalized":
        return 0
    if solver_owner_is_active(session, epoch):
        return 0
    units = units_from_solver_snapshot(session, epoch)
    if not units:
        raise RuntimeError("search_solver_snapshot_binding_missing")
    _abandon_units(
        session,
        epoch,
        units=units,
        now_value=now_value,
        reason="search_solver_owner_lost",
    )
    return 0


def _finalize_epoch(
    session: Session, context: _FinalizeContext,
) -> dict[str, int]:
    _restart_serializable_finalize_transaction(session)
    if not solver_owner_is_active(session, context.epoch):
        return _abandon_epoch(session, context, "search_solver_owner_lost")
    if not _snapshot_still_matches(session, context):
        return _abandon_epoch(session, context, "search_solver_input_changed")
    window = _lock_finalize_inputs(session, context)
    if window is None or window.bucket_end <= context.now:
        return _abandon_epoch(session, context, "search_solver_window_expired")
    if (window.allocation_state != "ready"
            or window.allocation_epoch != context.epoch.dispatch_allocation_epoch):
        return _abandon_epoch(session, context, "search_solver_epoch_superseded")
    matches = {item.obligation_id: item for item in context.result.matches}
    created_by_task: dict[str, int] = {}
    released_units: list[SearchClickFulfillmentUnit] = []
    for unit in context.units:
        match = matches.get(unit.obligation_id)
        path = context.paths.get(match.candidate_key) if match else None
        if path is None:
            released_units.append(unit)
            continue
        _bind_assignment_action(session, context.epoch, unit=unit,
                                path=path, now_value=context.now)
        created_by_task[unit.task_id] = created_by_task.get(unit.task_id, 0) + 1
    release_facts = _release_first_outcome_units(
        session, context.epoch, units=released_units, now_value=context.now,
        reason_code="no_feasible_search_path")
    context.epoch.outcome = context.result.outcome
    context.epoch.matched_unit_count = sum(created_by_task.values())
    context.epoch.released_unit_count = len(released_units)
    context.epoch.release_unit_set_hash = release_unit_set_hash(release_facts)
    matched_tasks = [
        unit.task_id for unit in context.units
        if unit.obligation_id in matches
    ]
    record_task_opportunities(session, matched_tasks, now_value=context.now)
    context.epoch.outcome_hash = finalize_search_outcome_hash(
        session, context.epoch,
        solver_result={
            "outcome": context.result.outcome,
            "solver_outcome_hash": context.result.outcome_hash,
            "matched_obligation_ids": sorted(matches),
        },
    )
    context.epoch.finalize_status = "finalized"
    context.epoch.finalized_at = context.now
    return created_by_task


def _restart_serializable_finalize_transaction(session: Session) -> None:
    session.rollback()
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))


def _lock_finalize_inputs(
    session: Session,
    context: _FinalizeContext,
) -> DispatchClaimWindow | None:
    window = session.scalar(
        select(DispatchClaimWindow)
        .where(DispatchClaimWindow.id == context.epoch.dispatch_claim_window_id)
        .with_for_update()
    )
    reservation_ids = sorted({unit.reservation_id for unit in context.units})
    obligation_ids = sorted({unit.obligation_id for unit in context.units})
    list(session.scalars(
        select(DispatchClaimReservation)
        .where(DispatchClaimReservation.id.in_(reservation_ids))
        .order_by(DispatchClaimReservation.id)
        .with_for_update()
    ))
    list(session.scalars(
        select(SearchClickFulfillmentObligation)
        .where(SearchClickFulfillmentObligation.id.in_(obligation_ids))
        .order_by(SearchClickFulfillmentObligation.id)
        .with_for_update()
    ))
    return window


def _snapshot_still_matches(
    session: Session,
    context: _FinalizeContext,
) -> bool:
    current_paths = candidate_paths(session, context.units, context.now)
    demands = search_click_demands(session, context.units)
    current = assemble_search_solver_snapshot(
        session,
        context.units,
        demands=demands,
        paths=tuple(item.candidate for item in current_paths.values()),
    )
    return (
        current.problem_hash == context.epoch.solver_problem_hash
        and current.input_hash == context.epoch.solver_input_hash
    )


def _bind_assignment_action(
    session: Session,
    epoch: SearchClickAssignmentEpoch,
    *,
    unit: SearchClickFulfillmentUnit,
    path: SearchClickPathContext,
    now_value: datetime,
) -> None:
    obligation = session.get(SearchClickFulfillmentObligation, unit.obligation_id)
    reservation = session.get(DispatchClaimReservation, unit.reservation_id)
    if obligation is None or reservation is None or obligation.status != "open":
        raise RuntimeError("search_click_assignment_precondition_lost")
    snapshot_hash = _path_snapshot_hash(path.candidate)
    assignment = SearchClickOpportunityAssignment(
        tenant_id=path.payload_input.account.tenant_id,
        task_id=unit.task_id,
        task_day_ledger_id=obligation.task_day_ledger_id,
        obligation_id=obligation.id,
        search_click_assignment_epoch_id=epoch.id,
        dispatch_claim_reservation_id=reservation.id,
        fulfillment_lane_claim_ordinal=unit.fulfillment_lane_claim_ordinal,
        account_id=path.candidate.account_id,
        authorization_id=path.candidate.authorization_id,
        keyword_hash=path.candidate.keyword_hash,
        proxy_route_id=path.candidate.proxy_route_id,
        protocol_sample_version=path.candidate.protocol_sample_version,
        resource_snapshot_hash=snapshot_hash,
    )
    session.add(assignment)
    session.flush()
    context = _AssignmentContext(
        unit,
        path,
        assignment,
        obligation,
        reservation,
        now_value,
    )
    action = _create_bound_action(session, context)
    _mark_assignment_bound(context, action)


def _create_bound_action(
    session: Session,
    context: _AssignmentContext,
) -> Action:
    task = session.get(Task, context.unit.task_id)
    if task is None:
        raise RuntimeError("search_click_assignment_task_missing")
    action = create_search_join_action(
        session,
        task,
        context.path.candidate.account_id,
        context.now,
        _payload(context.path.payload_input),
    )
    action.payload = {
        **(action.payload or {}),
        "search_click_obligation_id": context.obligation.id,
        "search_click_assignment_id": context.assignment.id,
        "dispatch_reservation_id": context.reservation.id,
        "fulfillment_lane_claim_ordinal":
            context.unit.fulfillment_lane_claim_ordinal,
    }
    action.result = {
        "search_click_assignment_id": context.assignment.id,
        "dispatch_prebound": True,
        "dispatch_reservation_id": context.reservation.id,
        "dispatch_claim_window_id": context.unit.window_id,
        "dispatch_allocation_epoch": context.unit.dispatch_allocation_epoch,
    }
    return action


def _mark_assignment_bound(
    context: _AssignmentContext,
    action: Action,
) -> None:
    context.assignment.action_id = action.id
    context.assignment.state = "action_bound"
    context.assignment.version += 1
    context.reservation.bound_count += 1
    context.reservation.version += 1
    context.obligation.source_action_id = action.id
    context.obligation.attempt_no += 1
    context.obligation.status = "action_bound"


def _release_first_outcome_units(
    session: Session, epoch: SearchClickAssignmentEpoch, *,
    units: list[SearchClickFulfillmentUnit], now_value: datetime,
    reason_code: str,
) -> list[dict]:
    release_facts: list[dict] = []
    for unit in units:
        reservation = session.get(DispatchClaimReservation, unit.reservation_id)
        allocation = session.get(
            DispatchClaimShardAllocation, reservation.dispatch_claim_shard_allocation_id,
        )
        component_hash = solver_component_hash_for_unit(
            session, epoch.id, unit.reservation_id,
            ordinal=unit.fulfillment_lane_claim_ordinal)
        session.add(DispatchAllocationExclusion(
            dispatch_claim_window_id=unit.window_id,
            dispatch_claim_reservation_id=unit.reservation_id,
            fulfillment_lane_claim_ordinal=unit.fulfillment_lane_claim_ordinal,
            carrier_type="search_click_assignment_epoch",
            carrier_id=epoch.id,
            reason_code=reason_code,
            solver_problem_component_hash=component_hash,
            resource_snapshot_hash=component_hash,
        ))
        release_facts.append({
            "window_id": unit.window_id,
            "reservation_id": unit.reservation_id,
            "ordinal": unit.fulfillment_lane_claim_ordinal,
            "reason_code": reason_code,
            "resource_snapshot_hash": component_hash,
        })
        reservation.released_count += 1
        reservation.version += 1
        allocation.unclaimed_allocated_count -= 1
        allocation.version += 1
        obligation = session.get(SearchClickFulfillmentObligation, unit.obligation_id)
        if obligation is not None:
            obligation.status = "open"
    if units:
        epoch.rebuild_input_version_after = start_or_join_dispatch_rebuild_wave(
            session,
            window_id=units[0].window_id,
            released_count=len(units),
            now_value=now_value,
        )
    return release_facts


def _abandon_epoch(
    session: Session,
    context: _FinalizeContext,
    reason: str,
) -> dict[str, int]:
    _abandon_units(
        session,
        context.epoch,
        units=context.units,
        now_value=context.now,
        reason=reason,
    )
    return {}


def _abandon_units(
    session: Session,
    epoch: SearchClickAssignmentEpoch,
    *,
    units,
    now_value: datetime,
    reason: str,
) -> None:
    unit_values = list(units)
    release_facts = _release_first_outcome_units(
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


def _path_snapshot_hash(candidate: SearchClickCandidatePath) -> str:
    return hashlib.sha256(repr(candidate).encode()).hexdigest()
__all__ = ["build_plan"]
