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
    dispatch_rebuild_input_hash,
    request_window_rebuild,
)
from .dispatch_claim_ledger import (
    confirm_dispatch_claim,
    current_window_allocations,
    dispatcher_claim_capacity,
    dispatcher_scope,
    reconcile_scope_active,
    reconcile_window_active,
    release_dispatch_claim,
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
    tasks = tasks_by_id(session, actions)
    demands = build_demands(actions, tasks, shard_total, now)
    if not demands:
        return DispatchClaimPlan((), {})
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
    released_count += scope_release_count
    released_count += int(window.pending_rebuild_release_count or 0)
    if window.unclaimed_allocated_count > 0:
        released_count = 0
    if released_count:
        request_window_rebuild(
            window,
            released_count=released_count,
            rebuild_input_hash=dispatch_rebuild_input_hash(
                scope,
                window,
                demands,
                released_count=released_count,
            ),
        )
    sync_window_capacity(window, capacity)
    if window.allocation_state != "ready":
        allocate_window(session, scope, window, all_allocations, demands)
    reservations = window_reservations(session, window.id)
    return plan_from_reservations(
        tasks,
        demands,
        reservations,
        window,
        shard_total,
        shard_index,
        fairness_decisions,
    )


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
