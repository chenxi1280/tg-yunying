from __future__ import annotations

from datetime import datetime
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, DispatchClaimReservation, DispatchClaimShardAllocation, DispatchClaimWindow, Task

from .dispatch_claim_ledger import reservation_available
from .dispatch_claim_types import (
    GROUP_AI_ADMISSION_ACTION_TYPES,
    GROUP_BOT_ADMISSION_ACTION_TYPES,
    SEARCH_MEMBERSHIP_CLAIM_CLASS,
    TARGET_ADMISSION_CLAIM_CLASS,
    TARGET_ADMISSION_RETRY_TASK_TYPE,
)
from .dispatch_fairness import classify_action_payload


def reconcile_window_unclaimed(
    session: Session,
    window: DispatchClaimWindow,
    *,
    allocations: list[DispatchClaimShardAllocation],
    reservations: Mapping[tuple[int, str, str, int, int], DispatchClaimReservation],
    now: datetime,
) -> None:
    due_counts = _due_reservation_action_counts(session, reservations, now)
    _release_stale_unclaimed_reservations(reservations, due_counts)
    _sync_window_unclaimed_counts(window, allocations, reservations)


def claim_class_for_action(task: Task, action: Action) -> str:
    payload = action.payload if isinstance(action.payload, dict) else {}
    if task.type == TARGET_ADMISSION_RETRY_TASK_TYPE:
        return TARGET_ADMISSION_CLAIM_CLASS
    if task.type == "group_ai_chat" and action.action_type in GROUP_AI_ADMISSION_ACTION_TYPES:
        return TARGET_ADMISSION_CLAIM_CLASS
    if action.action_type in GROUP_BOT_ADMISSION_ACTION_TYPES and _bound_admission_payload(payload):
        return TARGET_ADMISSION_CLAIM_CLASS
    if action.action_type == SEARCH_MEMBERSHIP_CLAIM_CLASS:
        return SEARCH_MEMBERSHIP_CLAIM_CLASS
    return classify_action_payload(action.action_type, payload, task.type)


def account_shard_for_action(action: Action, shard_total: int) -> int:
    account_id = int(action.account_id or 0)
    return account_id % shard_total if account_id else 0


def _due_reservation_action_counts(
    session: Session,
    reservations: Mapping[tuple[int, str, str, int, int], DispatchClaimReservation],
    now: datetime,
) -> dict[tuple[int, str, str, int, int], int]:
    task_ids = {reservation.task_id for reservation in reservations.values()}
    if not task_ids:
        return {}
    statement = select(Action, Task).join(Task, Task.id == Action.task_id).where(
        Action.task_id.in_(task_ids),
        Action.status == "pending",
        Action.scheduled_at <= now,
        Task.status == "running",
        Task.deleted_at.is_(None),
    )
    counts: dict[tuple[int, str, str, int, int], int] = {}
    for action, task in session.execute(statement):
        key = _due_reservation_key(action, task, reservations)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    return counts


def _due_reservation_key(
    action: Action,
    task: Task,
    reservations: Mapping[tuple[int, str, str, int, int], DispatchClaimReservation],
) -> tuple[int, str, str, int, int] | None:
    claim_class = claim_class_for_action(task, action)
    for key in reservations:
        tenant_id, task_id, reserved_class, shard_total, shard_index = key
        if (tenant_id, task_id, reserved_class) == (action.tenant_id, action.task_id, claim_class):
            if account_shard_for_action(action, shard_total) == shard_index:
                return key
    return None


def _release_stale_unclaimed_reservations(
    reservations: Mapping[tuple[int, str, str, int, int], DispatchClaimReservation],
    due_counts: Mapping[tuple[int, str, str, int, int], int],
) -> None:
    for key, reservation in reservations.items():
        available = reservation_available(reservation)
        retained = min(available, int(due_counts.get(key, 0)))
        if available > retained:
            reservation.reserved_claims -= available - retained
            reservation.reason = "unclaimed_action_no_longer_due"
            reservation.version += 1


def _sync_window_unclaimed_counts(
    window: DispatchClaimWindow,
    allocations: list[DispatchClaimShardAllocation],
    reservations: Mapping[tuple[int, str, str, int, int], DispatchClaimReservation],
) -> None:
    counts = {allocation.id: 0 for allocation in allocations}
    for reservation in reservations.values():
        counts[reservation.dispatch_claim_shard_allocation_id] = counts.get(
            reservation.dispatch_claim_shard_allocation_id, 0,
        ) + reservation_available(reservation)
    for allocation in allocations:
        expected = counts[allocation.id]
        if allocation.unclaimed_allocated_count != expected:
            allocation.unclaimed_allocated_count = expected
            allocation.version += 1
    expected = sum(counts.values())
    if window.unclaimed_allocated_count != expected:
        window.unclaimed_allocated_count = expected
        window.version += 1


def _bound_admission_payload(payload: Mapping[str, object]) -> bool:
    return bool(payload.get("admission_bound_task_id") and payload.get("admission_bound_account_id"))
