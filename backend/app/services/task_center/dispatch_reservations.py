"""Scope-wide Dispatcher claim orchestration.

Storage, allocation, and action selection live in focused modules so their
transactional invariants stay independently testable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from sqlalchemy.orm import Session

from app.models import Action

from .dispatch_claim_allocation import allocate_window
from .dispatch_claim_ledger import (
    confirm_dispatch_claim,
    dispatcher_claim_capacity,
    dispatcher_scope,
    reconcile_scope_active,
    reconcile_window_active,
    release_dispatch_claim,
    scope_for_update,
    sync_scope_capacity,
    task_dispatch_claim_snapshot,
    window_allocations,
    window_for_update,
    window_reservations,
)
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
    reconcile_scope_active(session, scope)
    sync_scope_capacity(scope, capacity)
    window = window_for_update(session, scope_name, now, capacity)
    allocations = window_allocations(session, window.id)
    reconcile_window_active(window, allocations)
    if window.unclaimed_allocated_count == 0:
        allocate_window(session, scope, window, allocations, demands)
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
