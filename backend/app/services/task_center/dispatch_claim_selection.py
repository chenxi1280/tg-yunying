from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, DispatchClaimReservation, DispatchClaimWindow, Task
from app.timezone import as_beijing_aware

from .dispatch_claim_allocation import normal_demands, rotated_demands, rotation_value, strict_non_priority_demands
from .dispatch_claim_ledger import reservation_available
from .dispatch_claim_reconciliation import account_shard_for_action, claim_class_for_action
from .dispatch_claim_types import (
    DispatchClaimBinding,
    DispatchClaimDemand,
    DispatchClaimPlan,
    HARD_HOURLY_CLAIM_CLASS,
    PRIORITY_CLAIM_CLASSES,
    SEARCH_MEMBERSHIP_CLAIM_CLASS,
    SEARCH_SOURCE_CLAIM_CLASS,
    SHARED_CAPACITY_ERROR,
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
    _write_unserved_task_stats(tasks, demands, reservations, window, unserved)
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
    )


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


def _write_unserved_task_stats(
    tasks: Mapping[str, Task],
    demands: list[DispatchClaimDemand],
    reservations: Mapping[tuple[int, str, str, int, int], DispatchClaimReservation],
    window: DispatchClaimWindow,
    unserved: Mapping[tuple[int, int], tuple[str, ...]],
) -> None:
    for demand in demands:
        available = reservation_available(reservations.get(demand.key))
        if demand.is_strict and available < demand.required_claims:
            task = tasks.get(demand.task_id)
            if task is not None:
                _set_shared_capacity_block(task, demand, window, available, unserved)


def _set_shared_capacity_block(
    task: Task,
    demand: DispatchClaimDemand,
    window: DispatchClaimWindow,
    available: int,
    unserved: Mapping[tuple[int, int], tuple[str, ...]],
) -> None:
    shard = (demand.shard_total, demand.shard_index)
    task.stats = {
        **(task.stats or {}),
        "dispatch_claim": {
            "status": SHARED_CAPACITY_ERROR,
            "dispatcher_scope": window.dispatcher_scope,
            "shard_total": demand.shard_total,
            "shard_index": demand.shard_index,
            "allocation_epoch": window.allocation_epoch,
            "required_claims": demand.required_claims,
            "available_claims": available,
            "unserved_strict_classes": list(unserved.get(shard, ())),
        },
    }
    task.last_error = SHARED_CAPACITY_ERROR


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
    priority = _take_priority_actions(action_ids, demands, epoch)
    strict = _take_balanced_actions(action_ids, strict_non_priority_demands(demands), reservations, epoch)
    ordinary = _take_balanced_actions(action_ids, normal_demands(demands), reservations, epoch)
    return priority + strict + ordinary


def _action_ids_by_demand(
    selected: Mapping[str, DispatchClaimDemand],
) -> dict[tuple[int, str, str, int, int], list[str]]:
    values: dict[tuple[int, str, str, int, int], list[str]] = defaultdict(list)
    for action_id, demand in selected.items():
        values[demand.key].append(action_id)
    return values


def _take_priority_actions(
    action_ids: dict[tuple[int, str, str, int, int], list[str]],
    demands: list[DispatchClaimDemand],
    epoch: int,
) -> list[str]:
    result: list[str] = []
    for claim_class in PRIORITY_CLAIM_CLASSES:
        rows = [demand for demand in demands if demand.claim_class == claim_class and demand.is_strict]
        for demand in rotated_demands(rows, epoch):
            result.extend(action_ids.pop(demand.key, []))
    return result


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
    result = list(ordered)
    for tenant_id, decision in fairness_decisions.items():
        if getattr(decision, "preferred_class", None) == "ordinary":
            _promote_tenant_ordinary(result, selected, int(tenant_id))
    return result


def _promote_tenant_ordinary(
    ordered: list[str],
    selected: Mapping[str, DispatchClaimDemand],
    tenant_id: int,
) -> None:
    hard_index = _first_index(ordered, selected, tenant_id, HARD_HOURLY_CLAIM_CLASS)
    ordinary_index = _first_index(ordered, selected, tenant_id, "ordinary")
    if hard_index is not None and ordinary_index is not None and ordinary_index > hard_index:
        ordered.insert(hard_index, ordered.pop(ordinary_index))


def _first_index(
    ordered: list[str],
    selected: Mapping[str, DispatchClaimDemand],
    tenant_id: int,
    claim_class: str,
) -> int | None:
    for index, action_id in enumerate(ordered):
        demand = selected[action_id]
        if demand.tenant_id == tenant_id and demand.claim_class == claim_class:
            return index
    return None


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
