from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, DispatchClaimReservation, DispatchClaimWindow, Task
from app.timezone import as_beijing_aware

from .dispatch_claim_allocation import rotation_value
from .dispatch_claim_ledger import reservation_available
from .dispatch_claim_reconciliation import account_shard_for_action, claim_class_for_action
from .dispatch_claim_types import (
    DispatchClaimBinding,
    DispatchClaimDemand,
    DispatchClaimPlan,
    HARD_HOURLY_CLAIM_CLASS,
    SEARCH_MEMBERSHIP_CLAIM_CLASS,
    SEARCH_SOURCE_CLAIM_CLASS,
    TARGET_ADMISSION_CLAIM_CLASS,
)


def tasks_by_id(session: Session, actions: list[Action]) -> dict[str, Task]:
    task_ids = {action.task_id for action in actions if action.task_id}
    if not task_ids:
        return {}
    return {task.id: task for task in session.scalars(select(Task).where(Task.id.in_(task_ids)))}


def build_demands(
    actions: list[Action],
    tasks: Mapping[str, Task],
    shard_total: int,
    now: datetime,
) -> list[DispatchClaimDemand]:
    grouped: dict[tuple[int, str, str, int, int], list[Action]] = defaultdict(list)
    for action in sorted(actions, key=_action_order_key):
        task = tasks.get(action.task_id)
        if task is None:
            continue
        claim_class = claim_class_for_action(task, action)
        key = (action.tenant_id, action.task_id, claim_class, shard_total, account_shard_for_action(action, shard_total))
        grouped[key].append(action)
    return [_demand_from_group(key, rows, tasks[key[1]], now) for key, rows in grouped.items()]


def plan_from_reservations(
    tasks: Mapping[str, Task],
    demands: list[DispatchClaimDemand],
    reservations: Mapping[tuple[int, str, str, int, int], DispatchClaimReservation],
    window: DispatchClaimWindow,
    shard_total: int,
    shard_index: int,
    fairness_decisions: Mapping[int, object],
) -> DispatchClaimPlan:
    unserved = _unserved_classes(demands, reservations)
    selected = _selected_actions(demands, reservations, shard_total, shard_index)
    ordered = _order_selected_actions(selected, demands, reservations, window.allocation_epoch)
    ordered = _apply_fairness_preference(ordered, selected, fairness_decisions)
    bindings = _bindings_for_actions(ordered, selected, reservations, window, unserved)
    return DispatchClaimPlan(tuple(ordered), bindings)


def _demand_from_group(
    key: tuple[int, str, str, int, int],
    actions: list[Action],
    task: Task,
    now: datetime,
) -> DispatchClaimDemand:
    tenant_id, task_id, claim_class, shard_total, shard_index = key
    business_task_id = _allocation_business_task_id(task_id, actions)
    return DispatchClaimDemand(
        tenant_id=tenant_id,
        task_id=task_id,
        claim_class=claim_class,
        shard_total=shard_total,
        shard_index=shard_index,
        action_ids=tuple(action.id for action in actions),
        required_claims=len(actions),
        urgency_score=_urgency_score(task, actions, claim_class, now),
        is_strict=_is_strict_claim(task, claim_class),
        allocation_business_task_id=business_task_id,
        lane_business_kind=_lane_business_kind(claim_class),
    )


def _allocation_business_task_id(
    task_id: str,
    actions: list[Action],
) -> str:
    for action in actions:
        payload = action.payload if isinstance(action.payload, dict) else {}
        sponsor = payload.get("admission_execution_sponsor_task_id")
        parent = payload.get("parent_task_id")
        if sponsor or parent:
            return str(sponsor or parent)
    return task_id


def _lane_business_kind(claim_class: str) -> str:
    if claim_class == TARGET_ADMISSION_CLAIM_CLASS:
        return "membership_admission"
    return "fulfillment"


def _is_strict_claim(task: Task, claim_class: str) -> bool:
    if claim_class in {TARGET_ADMISSION_CLAIM_CLASS, SEARCH_MEMBERSHIP_CLAIM_CLASS, HARD_HOURLY_CLAIM_CLASS}:
        return True
    config = task.type_config if isinstance(task.type_config, dict) else {}
    return claim_class == SEARCH_SOURCE_CLAIM_CLASS and bool(config.get("strict_daily_target"))


def _action_order_key(action: Action) -> tuple[datetime, datetime, str]:
    return (action.scheduled_at, action.created_at, action.id)


def _urgency_score(task: Task, actions: list[Action], claim_class: str, now: datetime) -> int:
    if claim_class == TARGET_ADMISSION_CLAIM_CLASS:
        return 1_000_000
    if claim_class == SEARCH_MEMBERSHIP_CLAIM_CLASS:
        return 500_000
    earliest_scheduled_at = min(action.scheduled_at for action in actions)
    overdue_seconds = max(0, int((as_beijing_aware(now) - as_beijing_aware(earliest_scheduled_at)).total_seconds()))
    return len(actions) * 100 + _task_target_weight(task, claim_class) + overdue_seconds


def _task_target_weight(task: Task, claim_class: str) -> int:
    config = task.type_config if isinstance(task.type_config, dict) else {}
    stats = task.stats if isinstance(task.stats, dict) else {}
    if claim_class == SEARCH_SOURCE_CLAIM_CLASS:
        return max(0, int(config.get("daily_click_target_count") or 0) - int(stats.get("target_click_observed") or 0))
    if claim_class == HARD_HOURLY_CLAIM_CLASS:
        return max(0, int(stats.get("hard_hourly_backfill_planning_deficit") or 0))
    return 0


def _unserved_classes(
    demands: list[DispatchClaimDemand],
    reservations: Mapping[tuple[int, str, str, int, int], DispatchClaimReservation],
) -> dict[tuple[int, int], tuple[str, ...]]:
    values: dict[tuple[int, int], set[str]] = defaultdict(set)
    for demand in demands:
        if demand.is_strict and reservation_available(reservations.get(demand.key)) < demand.required_claims:
            values[(demand.shard_total, demand.shard_index)].add(demand.claim_class)
    return {key: tuple(sorted(classes)) for key, classes in values.items()}


def _selected_actions(
    demands: list[DispatchClaimDemand],
    reservations: Mapping[tuple[int, str, str, int, int], DispatchClaimReservation],
    shard_total: int,
    shard_index: int,
) -> dict[str, DispatchClaimDemand]:
    selected: dict[str, DispatchClaimDemand] = {}
    for demand in demands:
        if (demand.shard_total, demand.shard_index) == (shard_total, shard_index):
            for action_id in demand.action_ids[: reservation_available(reservations.get(demand.key))]:
                selected[action_id] = demand
    return selected


def _order_selected_actions(
    selected: Mapping[str, DispatchClaimDemand],
    demands: list[DispatchClaimDemand],
    reservations: Mapping[tuple[int, str, str, int, int], DispatchClaimReservation],
    epoch: int,
) -> list[str]:
    action_ids = _action_ids_by_demand(selected)
    return _take_balanced_actions(
        action_ids,
        demands,
        reservations,
        epoch,
    )


def _action_ids_by_demand(
    selected: Mapping[str, DispatchClaimDemand],
) -> dict[tuple[int, str, str, int, int], list[str]]:
    values: dict[tuple[int, str, str, int, int], list[str]] = defaultdict(list)
    for action_id, demand in selected.items():
        values[demand.key].append(action_id)
    return values


def _take_balanced_actions(
    action_ids: dict[tuple[int, str, str, int, int], list[str]],
    demands: list[DispatchClaimDemand],
    reservations: Mapping[tuple[int, str, str, int, int], DispatchClaimReservation],
    epoch: int,
) -> list[str]:
    result: list[str] = []
    while available := [demand for demand in demands if action_ids.get(demand.key)]:
        demand = min(available, key=lambda row: _consumption_order_key(row, reservations.get(row.key), epoch))
        result.append(action_ids[demand.key].pop(0))
    return result


def _consumption_order_key(
    demand: DispatchClaimDemand,
    reservation: DispatchClaimReservation | None,
    epoch: int,
) -> tuple[float, int, int]:
    claimed = int(reservation.claimed_count) if reservation is not None else 0
    reserved = max(1, int(reservation.reserved_claims) if reservation is not None else 1)
    return (float(claimed) / float(reserved), -demand.urgency_score, rotation_value(demand, epoch))


def _apply_fairness_preference(
    ordered: list[str],
    selected: Mapping[str, DispatchClaimDemand],
    fairness_decisions: Mapping[int, object],
) -> list[str]:
    del selected, fairness_decisions
    return ordered


def _bindings_for_actions(
    action_ids: list[str],
    selected: Mapping[str, DispatchClaimDemand],
    reservations: Mapping[tuple[int, str, str, int, int], DispatchClaimReservation],
    window: DispatchClaimWindow,
    unserved: Mapping[tuple[int, int], tuple[str, ...]],
) -> dict[str, DispatchClaimBinding]:
    result: dict[str, DispatchClaimBinding] = {}
    for action_id in action_ids:
        demand = selected[action_id]
        reservation = reservations[demand.key]
        result[action_id] = DispatchClaimBinding(
            reservation_id=reservation.id,
            window_id=window.id,
            shard_allocation_id=reservation.dispatch_claim_shard_allocation_id,
            dispatcher_scope=window.dispatcher_scope,
            shard_total=demand.shard_total,
            shard_index=demand.shard_index,
            allocation_epoch=window.allocation_epoch,
            claim_class=demand.claim_class,
            reservation_reason=reservation.reason,
            urgency_score=reservation.urgency_score,
            unserved_strict_classes=unserved.get((demand.shard_total, demand.shard_index), ()),
        )
    return result
