"""Scope-wide Dispatcher claim orchestration.

Storage, allocation, and action selection live in focused modules so their
transactional invariants stay independently testable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from sqlalchemy.orm import Session

from app.models import Action

from .dispatch_claim_allocation import (
    allocate_window,
    dispatch_demand_hash,
    dispatch_rebuild_input_hash,
    request_window_rebuild,
)
from .dispatch_claim_ledger import (
    confirm_dispatch_claim as _confirm_dispatch_claim,
    current_window_allocations,
    dispatcher_claim_capacity,
    dispatcher_scope,
    reconcile_scope_active,
    reconcile_window_active,
    release_dispatch_claim,
    reservation_available,
    scope_for_update,
    sync_scope_capacity,
    sync_window_capacity,
    task_dispatch_claim_snapshot,
    window_allocations,
    window_for_update,
    window_reservations,
)
from .dispatch_claim_reconciliation import reconcile_window_unclaimed
from .dispatch_claim_selection import build_demands, plan_from_reservations, tasks_by_id
from .dispatch_claim_types import DispatchClaimBinding, DispatchClaimPlan
from .prebound_search_claim import (
    confirm_prebound_search_claim,
    plan_prebound_search_claims,
)


def plan_dispatch_claims(
    session: Session,
    actions: list[Action],
    *,
    settings,
    now: datetime,
    shard_total: int,
    shard_index: int,
    fairness_decisions: Mapping[int, object],
) -> DispatchClaimPlan:
    if not actions:
        return DispatchClaimPlan((), {})
    prebound = plan_prebound_search_claims(session, actions)
    remaining = _unbound_actions(actions, prebound)
    if not remaining:
        return prebound
    tasks = tasks_by_id(session, remaining)
    demands = build_demands(remaining, tasks, shard_total, now)
    if not demands:
        return DispatchClaimPlan((), {})
    scope, window, all_allocations = _prepare_dispatch_window(
        session,
        actions,
        demands,
        settings=settings,
        now=now,
    )
    reservations = window_reservations(session, window.id)
    allocated = plan_from_reservations(
        tasks,
        demands,
        reservations,
        window,
        shard_total,
        shard_index,
        fairness_decisions,
    )
    return _combine_claim_plans(prebound, allocated)


def _prepare_dispatch_window(
    session: Session,
    actions: list[Action],
    demands,
    *,
    settings,
    now: datetime,
):
    scope_name = dispatcher_scope(settings)
    capacity = dispatcher_claim_capacity(settings, len(actions))
    scope = scope_for_update(session, scope_name, capacity)
    active_actions = reconcile_scope_active(session, scope)
    sync_scope_capacity(scope, capacity)
    window = window_for_update(session, scope_name, now, capacity)
    scope_release_count = max(
        0,
        int(window.allocation_scope_active_count or 0)
        - int(scope.active_claim_count),
    )
    all_allocations = window_allocations(session, window.id)
    released_count, reservations = _reconciled_release_count(
        session,
        window=window,
        all_allocations=all_allocations,
        active_actions=active_actions,
        now=now,
    )
    released_count += scope_release_count
    if window.unclaimed_allocated_count > 0 and reservations:
        released_count = 0
    _request_rebuild_if_needed(
        scope=scope,
        window=window,
        demands=demands,
        reservations=reservations,
        released_count=released_count,
    )
    sync_window_capacity(window, capacity)
    if window.allocation_state != "ready":
        allocate_window(session, scope, window, all_allocations, demands)
    return scope, window, all_allocations


def _request_rebuild_if_needed(
    *,
    scope,
    window,
    demands,
    reservations,
    released_count: int,
) -> None:
    demand_hash = dispatch_demand_hash(demands)
    demand_without_reservation = any(
        reservation_available(reservations.get(demand.key)) <= 0
        for demand in demands
    )
    input_changed = _input_change_requires_rebuild(
        window,
        demand_hash=demand_hash,
        demand_without_reservation=demand_without_reservation,
    )
    if released_count or input_changed:
        request_window_rebuild(
            window,
            released_count=released_count,
            rebuild_input_hash=dispatch_rebuild_input_hash(
                scope,
                window,
                demands,
                released_count=released_count,
            ),
            input_changed=input_changed,
        )


def _reconciled_release_count(
    session: Session,
    *,
    window,
    all_allocations,
    active_actions,
    now: datetime,
):
    active_release_count = reconcile_window_active(
        window,
        all_allocations,
        active_actions,
    )
    allocations = current_window_allocations(session, window)
    reservations = window_reservations(session, window.id)
    released_count = reconcile_window_unclaimed(
        session,
        window,
        allocations=allocations,
        reservations=reservations,
        now=now,
    )
    released_count += active_release_count
    released_count += int(window.pending_rebuild_release_count or 0)
    return released_count, reservations


def _unbound_actions(
    actions: list[Action],
    prebound: DispatchClaimPlan,
) -> list[Action]:
    return [
        action
        for action in actions
        if action.id not in prebound.bindings_by_action_id
        and not (action.result or {}).get("dispatch_prebound")
    ]


def _input_change_requires_rebuild(
    window,
    *,
    demand_hash: str,
    demand_without_reservation: bool,
) -> bool:
    if window.allocation_state != "ready":
        return False
    if int(window.unclaimed_allocated_count or 0) > 0:
        return False
    return (
        window.ready_rebuild_snapshot_hash != demand_hash
        or demand_without_reservation
    )


def _combine_claim_plans(
    prebound: DispatchClaimPlan,
    allocated: DispatchClaimPlan,
) -> DispatchClaimPlan:
    action_ids = tuple(dict.fromkeys(
        (*prebound.candidate_action_ids, *allocated.candidate_action_ids)
    ))
    bindings = {
        **allocated.bindings_by_action_id,
        **prebound.bindings_by_action_id,
    }
    return DispatchClaimPlan(action_ids, bindings)


def confirm_dispatch_claim(
    session: Session,
    action: Action,
    binding: DispatchClaimBinding,
) -> bool:
    result = action.result if isinstance(action.result, dict) else {}
    if result.get("dispatch_prebound"):
        return confirm_prebound_search_claim(session, action, binding)
    return _confirm_dispatch_claim(session, action, binding)


__all__ = [
    "DispatchClaimBinding",
    "DispatchClaimPlan",
    "confirm_dispatch_claim",
    "dispatcher_claim_capacity",
    "dispatcher_scope",
    "plan_dispatch_claims",
    "release_dispatch_claim",
    "task_dispatch_claim_snapshot",
]
