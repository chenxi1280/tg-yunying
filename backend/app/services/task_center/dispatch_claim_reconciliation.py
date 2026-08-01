from __future__ import annotations

from datetime import datetime
from typing import Mapping

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, load_only

from app.models import (
    Action,
    DispatchClaimReservation,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    SearchClickAssignmentEpoch,
    Task,
)

from .dispatch_claim_ledger import reservation_available
from .dispatch_claim_types import (
    GROUP_AI_ADMISSION_ACTION_TYPES,
    GROUP_BOT_ADMISSION_ACTION_TYPES,
    SEARCH_MEMBERSHIP_CLAIM_CLASS,
    SEARCH_SOURCE_CLAIM_CLASS,
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
    current_contract_version: str | None = None,
    runtime_shard_total: int | None = None,
) -> int:
    search_ids = _search_fulfillment_reservation_ids(session, reservations)
    stale_release_count, stale_keys = _release_stale_contract_reservations(
        session,
        allocations=allocations,
        reservations=reservations,
        search_reservation_ids=search_ids,
        now=now,
        current_contract_version=current_contract_version,
        runtime_shard_total=runtime_shard_total,
    )
    due_counts = _due_reservation_action_counts(session, reservations, now)
    for key in stale_keys:
        due_counts[key] = 0
    released_count = _release_stale_unclaimed_reservations(
        reservations,
        due_counts,
    )
    _sync_window_unclaimed_counts(window, allocations, reservations)
    return stale_release_count + released_count


def claim_class_for_action(task: Task, action: Action) -> str:
    payload = action.payload if isinstance(action.payload, dict) else {}
    if task.type == TARGET_ADMISSION_RETRY_TASK_TYPE:
        return TARGET_ADMISSION_CLAIM_CLASS
    if _is_daily_group_ai_admission(task, action):
        return TARGET_ADMISSION_CLAIM_CLASS
    if action.action_type in GROUP_BOT_ADMISSION_ACTION_TYPES and _bound_admission_payload(payload):
        return TARGET_ADMISSION_CLAIM_CLASS
    if action.action_type == SEARCH_MEMBERSHIP_CLAIM_CLASS:
        return SEARCH_MEMBERSHIP_CLAIM_CLASS
    return classify_action_payload(action.action_type, payload, task.type)


def account_shard_for_action(action: Action, shard_total: int) -> int:
    account_id = int(action.account_id or 0)
    return account_id % shard_total if account_id else 0


def _is_daily_group_ai_admission(task: Task, action: Action) -> bool:
    config = task.type_config if isinstance(task.type_config, dict) else {}
    return bool(
        task.type == "group_ai_chat"
        and not config.get("hard_hourly_target_enabled")
        and action.action_type in GROUP_AI_ADMISSION_ACTION_TYPES
    )


def _due_reservation_action_counts(
    session: Session,
    reservations: Mapping[tuple[int, str, str, int, int], DispatchClaimReservation],
    now: datetime,
) -> dict[tuple[int, str, str, int, int], int]:
    task_ids = {reservation.task_id for reservation in reservations.values()}
    if not task_ids:
        return {}
    statement = select(Action, Task).join(Task, Task.id == Action.task_id).options(
        load_only(
            Action.id,
            Action.tenant_id,
            Action.task_id,
            Action.task_type,
            Action.action_type,
            Action.account_id,
            Action.payload,
        ),
        load_only(
            Task.id,
            Task.tenant_id,
            Task.type,
            Task.type_config,
        ),
    ).where(
        Action.task_id.in_(task_ids),
        or_(
            Action.status == "pending",
            and_(
                Action.status == "claiming",
                Action.claim_expires_at > now,
            ),
        ),
        Action.scheduled_at <= now,
        Task.status == "running",
        Task.deleted_at.is_(None),
    )
    counts: dict[tuple[int, str, str, int, int], int] = {}
    for action, task in session.execute(statement):
        key = _due_reservation_key(action, task, reservations)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    _protect_search_reservations(
        session,
        reservations=reservations,
        counts=counts,
    )
    return counts


def _protect_search_reservations(
    session: Session,
    *,
    reservations: Mapping[
        tuple[int, str, str, int, int],
        DispatchClaimReservation,
    ],
    counts: dict[tuple[int, str, str, int, int], int],
) -> None:
    fulfillment_ids = _search_fulfillment_reservation_ids(session, reservations)
    finalized_ids = _finalized_search_reservation_ids(
        session,
        fulfillment_ids,
    )
    for key, reservation in reservations.items():
        if reservation.id not in fulfillment_ids:
            continue
        retained = int(reservation.bound_count or 0)
        if reservation.id not in finalized_ids:
            retained = reservation_available(reservation)
        counts[key] = max(counts.get(key, 0), retained)


def _finalized_search_reservation_ids(
    session: Session,
    reservation_ids: set[str],
) -> set[str]:
    if not reservation_ids:
        return set()
    return set(session.scalars(
        select(DispatchClaimReservation.id)
        .join(
            DispatchClaimShardAllocation,
            DispatchClaimShardAllocation.id
            == DispatchClaimReservation.dispatch_claim_shard_allocation_id,
        )
        .join(
            SearchClickAssignmentEpoch,
            and_(
                SearchClickAssignmentEpoch.dispatch_claim_window_id
                == DispatchClaimShardAllocation.dispatch_claim_window_id,
                SearchClickAssignmentEpoch.dispatch_allocation_epoch
                == DispatchClaimReservation.dispatch_allocation_epoch,
            ),
        )
        .where(
            DispatchClaimReservation.id.in_(reservation_ids),
            SearchClickAssignmentEpoch.finalize_status == "finalized",
        )
    ))


def _search_fulfillment_reservation_ids(
    session: Session,
    reservations: Mapping[
        tuple[int, str, str, int, int],
        DispatchClaimReservation,
    ],
) -> set[str]:
    source_ids = [
        row.id for row in reservations.values()
        if row.claim_class == SEARCH_SOURCE_CLAIM_CLASS
    ]
    if not source_ids:
        return set()
    return set(session.scalars(
        select(DispatchClaimReservation.id)
        .join(Task, Task.id == DispatchClaimReservation.task_id)
        .where(
            DispatchClaimReservation.id.in_(source_ids),
            Task.type == "search_click",
        )
    ))


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
) -> int:
    released_count = 0
    for key, reservation in reservations.items():
        available = reservation_available(reservation)
        retained = min(available, int(due_counts.get(key, 0)))
        if available > retained:
            released = available - retained
            reservation.reserved_claims -= released
            reservation.reason = "unclaimed_action_no_longer_due"
            reservation.version += 1
            released_count += released
    return released_count


def _release_stale_contract_reservations(
    session: Session,
    *,
    allocations: list[DispatchClaimShardAllocation],
    reservations: Mapping[
        tuple[int, str, str, int, int], DispatchClaimReservation,
    ],
    search_reservation_ids: set[str],
    now: datetime,
    current_contract_version: str | None,
    runtime_shard_total: int | None,
) -> tuple[int, set[tuple[int, str, str, int, int]]]:
    if current_contract_version is None or runtime_shard_total is None:
        return 0, set()
    allocation_by_id = {allocation.id: allocation for allocation in allocations}
    released_count = 0
    stale_keys: set[tuple[int, str, str, int, int]] = set()
    for key, reservation in reservations.items():
        if reservation.id in search_reservation_ids:
            continue
        allocation = allocation_by_id.get(
            reservation.dispatch_claim_shard_allocation_id,
        )
        if allocation is None or not _allocation_contract_is_stale(
            allocation,
            current_contract_version=current_contract_version,
            runtime_shard_total=runtime_shard_total,
        ):
            continue
        stale_keys.add(key)
        released = reservation_available(reservation)
        if released <= 0:
            continue
        reservation.reserved_claims -= released
        reservation.reason = "dispatch_binding_replan_required"
        reservation.version += 1
        released_count += released
        _mark_due_actions_for_replan(session, key, reservation, now)
    return released_count, stale_keys


def _allocation_contract_is_stale(
    allocation: DispatchClaimShardAllocation,
    *,
    current_contract_version: str,
    runtime_shard_total: int,
) -> bool:
    return bool(
        allocation.account_shard_total != runtime_shard_total
        or allocation.account_shard_index >= runtime_shard_total
        or allocation.dispatch_contract_version != current_contract_version
    )


def _mark_due_actions_for_replan(
    session: Session,
    key: tuple[int, str, str, int, int],
    reservation: DispatchClaimReservation,
    now: datetime,
) -> None:
    tenant_id, task_id, claim_class, shard_total, shard_index = key
    rows = session.execute(
        select(Action, Task)
        .join(Task, Task.id == Action.task_id)
        .where(
            Action.tenant_id == tenant_id,
            Action.task_id == task_id,
            Action.status.in_(("pending", "claiming")),
            Action.scheduled_at <= now,
            Task.status == "running",
            Task.deleted_at.is_(None),
        )
    )
    for action, task in rows:
        if claim_class_for_action(task, action) != claim_class:
            continue
        if account_shard_for_action(action, shard_total) != shard_index:
            continue
        action.result = _replan_result(action, reservation)


def _replan_result(
    action: Action,
    reservation: DispatchClaimReservation,
) -> dict:
    result = dict(action.result or {})
    for key in (
        "dispatch_claim_window_id",
        "dispatch_claim_shard_allocation_id",
        "dispatch_reservation_id",
        "dispatch_claim_shard",
        "dispatch_allocation_epoch",
    ):
        result.pop(key, None)
    return {
        **result,
        "dispatch_claim_active": False,
        "dispatch_binding_replan_required": True,
        "dispatch_binding_replan_source_reservation_id": reservation.id,
        "error_code": "dispatch_binding_replan_required",
    }


def _sync_window_unclaimed_counts(
    window: DispatchClaimWindow,
    allocations: list[DispatchClaimShardAllocation],
    reservations: Mapping[tuple[int, str, str, int, int], DispatchClaimReservation],
) -> None:
    counts = {allocation.id: 0 for allocation in allocations}
    for reservation in reservations.values():
        counts[reservation.dispatch_claim_shard_allocation_id] = counts.get(
            reservation.dispatch_claim_shard_allocation_id, 0,
        ) + _reservation_unclaimed_count(reservation)
    for allocation in allocations:
        expected = counts[allocation.id]
        if allocation.unclaimed_allocated_count != expected:
            allocation.unclaimed_allocated_count = expected
            allocation.version += 1
    expected = sum(counts.values())
    if window.unclaimed_allocated_count != expected:
        window.unclaimed_allocated_count = expected
        window.version += 1
    if window.effective_unclaimed_count != expected:
        window.effective_unclaimed_count = expected
        window.version += 1


def sync_window_unclaimed_total(
    window: DispatchClaimWindow,
    allocations: list[DispatchClaimShardAllocation],
) -> None:
    expected = sum(int(row.unclaimed_allocated_count) for row in allocations)
    if window.unclaimed_allocated_count != expected:
        window.unclaimed_allocated_count = expected
        window.version += 1
    if window.effective_unclaimed_count != expected:
        window.effective_unclaimed_count = expected
        window.version += 1


def _reservation_unclaimed_count(
    reservation: DispatchClaimReservation,
) -> int:
    unclaimed = (
        int(reservation.reserved_claims)
        - int(reservation.claimed_count)
        - int(reservation.released_count)
    )
    if unclaimed < int(reservation.bound_count):
        raise RuntimeError("dispatch_reconciliation_counter_invariant")
    return unclaimed


def _bound_admission_payload(payload: Mapping[str, object]) -> bool:
    return bool(payload.get("admission_bound_task_id") and payload.get("admission_bound_account_id"))
