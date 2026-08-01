from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    Action,
    DispatchAllocationExclusion,
    DispatchClaimReservation,
    DispatchClaimShardAllocation,
    SearchClickFulfillmentObligation,
    SearchClickOpportunityAssignment,
    Task,
    TaskDayLedger,
)

from .dispatch_claim_allocation import allocate_window
from .dispatch_claim_ledger import (
    dispatcher_claim_capacity,
    dispatcher_scope,
    reconcile_scope_active,
    scope_for_update,
    sync_scope_capacity,
    sync_window_capacity,
    window_allocations,
    window_for_update,
)
from .dispatch_claim_selection import build_demands, tasks_by_id
from .dispatch_claim_types import DispatchClaimDemand, SEARCH_SOURCE_CLAIM_CLASS
from .dispatch_reservations import runtime_allocation_limits


@dataclass(frozen=True)
class SearchClickFulfillmentUnit:
    obligation_id: str
    task_id: str
    reservation_id: str
    window_id: str
    dispatch_allocation_epoch: int
    fulfillment_lane_claim_ordinal: int


def prepare_search_click_fulfillment_units(
    session: Session,
    *,
    now: datetime,
) -> tuple[SearchClickFulfillmentUnit, ...]:
    settings = get_settings()
    scope_name = dispatcher_scope(settings)
    capacity = dispatcher_claim_capacity(settings, int(settings.action_claim_limit))
    scope = scope_for_update(session, scope_name, capacity)
    reconcile_scope_active(session, scope)
    sync_scope_capacity(scope, capacity)
    window = window_for_update(session, scope_name, now, capacity)
    shard_total = max(1, int(settings.dispatch_runtime_shard_total or 0))
    demands = _all_demands(session, now, shard_total=shard_total)
    sync_window_capacity(window, capacity)
    if window.allocation_state != "ready":
        allocations = window_allocations(session, window.id)
        runtime_limits = runtime_allocation_limits(
            session,
            scope,
            allocations,
            settings=settings,
            now=now,
        )
        live_limits, runtime_total = (
            runtime_limits if runtime_limits is not None else (None, None)
        )
        allocate_window(
            session,
            scope,
            window,
            allocations,
            demands,
            live_shard_available=live_limits,
            runtime_shard_total=runtime_total,
        )
    return _available_search_units(session, window.id, window.allocation_epoch)


def _all_demands(
    session: Session,
    now: datetime,
    *,
    shard_total: int,
) -> list[DispatchClaimDemand]:
    actions = list(session.scalars(
        select(Action)
        .join(Task, Task.id == Action.task_id)
        .where(
            Action.status == "pending",
            Action.scheduled_at <= now,
            Task.status == "running",
            Task.deleted_at.is_(None),
        )
        .order_by(Action.scheduled_at, Action.created_at, Action.id)
    ))
    tasks = tasks_by_id(session, actions)
    demands = build_demands(actions, tasks, shard_total, now)
    search_demands = open_search_click_dispatch_demands(session, now)
    keys = {demand.key for demand in search_demands}
    return [demand for demand in demands if demand.key not in keys] + search_demands


def open_search_click_dispatch_demands(
    session: Session,
    now: datetime,
) -> list[DispatchClaimDemand]:
    rows = session.execute(
        select(Task, TaskDayLedger, SearchClickFulfillmentObligation)
        .join(TaskDayLedger, TaskDayLedger.task_id == Task.id)
        .join(
            SearchClickFulfillmentObligation,
            SearchClickFulfillmentObligation.task_day_ledger_id == TaskDayLedger.id,
        )
        .where(
            Task.type == "search_click",
            Task.status == "running",
            Task.deleted_at.is_(None),
            TaskDayLedger.lifecycle_status == "open",
            TaskDayLedger.deadline_at > now,
            (Task.scheduled_end.is_(None) | (Task.scheduled_end > now)),
            SearchClickFulfillmentObligation.status == "open",
        )
        .order_by(Task.id, SearchClickFulfillmentObligation.click_obligation_ordinal)
    ).all()
    grouped: dict[str, tuple[Task, list[str]]] = {}
    for task, _, obligation in rows:
        grouped.setdefault(task.id, (task, []))[1].append(obligation.id)
    return [
        DispatchClaimDemand(
            tenant_id=task.tenant_id,
            task_id=task.id,
            claim_class=SEARCH_SOURCE_CLAIM_CLASS,
            shard_total=1,
            shard_index=0,
            action_ids=tuple(obligation_ids),
            required_claims=len(obligation_ids),
            urgency_score=len(obligation_ids) * 100,
            is_strict=True,
            allocation_business_task_id=task.id,
            lane_business_kind="fulfillment",
        )
        for task, obligation_ids in grouped.values()
    ]


def _available_search_units(
    session: Session,
    window_id: str,
    epoch: int,
) -> tuple[SearchClickFulfillmentUnit, ...]:
    reservations = list(session.scalars(
        select(DispatchClaimReservation)
        .join(
            DispatchClaimShardAllocation,
            DispatchClaimShardAllocation.id
            == DispatchClaimReservation.dispatch_claim_shard_allocation_id,
        )
        .where(
            DispatchClaimShardAllocation.dispatch_claim_window_id == window_id,
            DispatchClaimShardAllocation.dispatch_allocation_epoch == epoch,
            DispatchClaimReservation.claim_class == SEARCH_SOURCE_CLAIM_CLASS,
        )
        .order_by(DispatchClaimReservation.task_id, DispatchClaimReservation.id)
    ))
    result: list[SearchClickFulfillmentUnit] = []
    for reservation in reservations:
        result.extend(_reservation_units(
            session,
            reservation,
            window_id=window_id,
            epoch=epoch,
        ))
    return tuple(result)


def _reservation_units(
    session: Session,
    reservation: DispatchClaimReservation,
    *,
    window_id: str,
    epoch: int,
) -> list[SearchClickFulfillmentUnit]:
    occupied = set(session.scalars(
        select(SearchClickOpportunityAssignment.fulfillment_lane_claim_ordinal)
        .where(
            SearchClickOpportunityAssignment.dispatch_claim_reservation_id
            == reservation.id
        )
    ))
    occupied.update(session.scalars(
        select(DispatchAllocationExclusion.fulfillment_lane_claim_ordinal)
        .where(
            DispatchAllocationExclusion.dispatch_claim_reservation_id
            == reservation.id
        )
    ))
    obligations = list(session.scalars(
        select(SearchClickFulfillmentObligation)
        .join(
            TaskDayLedger,
            TaskDayLedger.id
            == SearchClickFulfillmentObligation.task_day_ledger_id,
        )
        .where(
            TaskDayLedger.task_id == reservation.task_id,
            TaskDayLedger.lifecycle_status == "open",
            SearchClickFulfillmentObligation.status == "open",
        )
        .order_by(SearchClickFulfillmentObligation.click_obligation_ordinal)
    ))
    ordinals = _unoccupied_ordinals(reservation, occupied)
    count = min(len(ordinals), len(obligations))
    return [
        SearchClickFulfillmentUnit(
            obligations[index].id,
            reservation.task_id,
            reservation.id,
            window_id,
            epoch,
            ordinals[index],
        )
        for index in range(count)
    ]


def _unoccupied_ordinals(
    reservation: DispatchClaimReservation,
    occupied: set[int],
) -> list[int]:
    return [
        ordinal
        for ordinal in range(1, int(reservation.reserved_claims) + 1)
        if ordinal not in occupied
    ]


__all__ = [
    "SearchClickFulfillmentUnit",
    "open_search_click_dispatch_demands",
    "prepare_search_click_fulfillment_units",
]
