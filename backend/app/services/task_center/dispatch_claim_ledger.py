from __future__ import annotations

from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Action,
    DispatchClaimReservation,
    DispatchClaimScope,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    Task,
)
from app.services._common import _now

from .dispatch_claim_contract import binding_metadata, dispatcher_claim_capacity, dispatcher_scope
from .dispatch_claim_types import CLAIM_WINDOW_SECONDS, DispatchClaimBinding


def scope_for_update(session: Session, scope: str, capacity: int) -> DispatchClaimScope:
    statement = select(DispatchClaimScope).where(DispatchClaimScope.dispatcher_scope == scope)
    ledger = session.scalar(for_update(session, statement))
    if ledger is not None:
        return ledger
    return _create_scope(session, scope, capacity)


def reconcile_scope_active(session: Session, scope: DispatchClaimScope) -> list[Action]:
    active_actions = _scope_active_claims(session, scope.dispatcher_scope)
    active_count = len(active_actions)
    if scope.active_claim_count != active_count:
        scope.active_claim_count = active_count
        scope.version += 1
    return active_actions


def sync_scope_capacity(scope: DispatchClaimScope, capacity: int) -> None:
    expected = max(int(scope.active_claim_count), capacity)
    if scope.claim_capacity != expected:
        scope.claim_capacity = expected
        scope.version += 1


def window_for_update(session: Session, scope: str, now: datetime, capacity: int) -> DispatchClaimWindow:
    bucket_start, bucket_end = _bucket_bounds(now)
    statement = select(DispatchClaimWindow).where(
        DispatchClaimWindow.dispatcher_scope == scope,
        DispatchClaimWindow.bucket_start == bucket_start,
        DispatchClaimWindow.bucket_end == bucket_end,
    )
    window = session.scalar(for_update(session, statement))
    if window is None:
        return _create_window(session, scope, bucket_start, bucket_end, capacity)
    return window


def window_allocations(session: Session, window_id: str) -> list[DispatchClaimShardAllocation]:
    statement = select(DispatchClaimShardAllocation).where(
        DispatchClaimShardAllocation.dispatch_claim_window_id == window_id,
    )
    return list(session.scalars(for_update(session, statement)))


def current_window_allocations(
    session: Session,
    window: DispatchClaimWindow,
) -> list[DispatchClaimShardAllocation]:
    statement = select(DispatchClaimShardAllocation).where(
        DispatchClaimShardAllocation.dispatch_claim_window_id == window.id,
        DispatchClaimShardAllocation.dispatch_allocation_epoch
        == window.allocation_epoch,
    )
    return list(session.scalars(for_update(session, statement)))


def window_reservations(
    session: Session,
    window_id: str,
    *,
    allocation_epoch: int | None = None,
) -> dict[tuple[int, str, str, int, int], DispatchClaimReservation]:
    window = session.get(DispatchClaimWindow, window_id)
    if window is None:
        return {}
    selected_epoch = window.allocation_epoch if allocation_epoch is None else allocation_epoch
    statement = select(DispatchClaimReservation, DispatchClaimShardAllocation).join(
        DispatchClaimShardAllocation,
        DispatchClaimShardAllocation.id == DispatchClaimReservation.dispatch_claim_shard_allocation_id,
    ).where(
        DispatchClaimShardAllocation.dispatch_claim_window_id == window_id,
        DispatchClaimShardAllocation.dispatch_allocation_epoch
        == selected_epoch,
    )
    rows = session.execute(for_update(session, statement)).all()
    return {reservation_key(reservation, allocation): reservation for reservation, allocation in rows}


def claimable_window_reservations(
    session: Session,
    window_id: str,
) -> dict[tuple[int, str, str, int, int], DispatchClaimReservation]:
    statement = (
        select(DispatchClaimReservation, DispatchClaimShardAllocation)
        .join(
            DispatchClaimShardAllocation,
            DispatchClaimShardAllocation.id
            == DispatchClaimReservation.dispatch_claim_shard_allocation_id,
        )
        .where(DispatchClaimShardAllocation.dispatch_claim_window_id == window_id)
        .order_by(
            DispatchClaimShardAllocation.dispatch_allocation_epoch.desc(),
            DispatchClaimReservation.id,
        )
    )
    result: dict[tuple[int, str, str, int, int], DispatchClaimReservation] = {}
    for reservation, allocation in session.execute(for_update(session, statement)):
        key = reservation_key(reservation, allocation)
        if key not in result and reservation_available(reservation) > 0:
            result[key] = reservation
    return result


def reconcile_window_active(
    window: DispatchClaimWindow,
    allocations: list[DispatchClaimShardAllocation],
    active_actions: list[Action],
) -> int:
    previous_active = int(window.active_claim_count)
    active_counts = _active_claim_counts_by_allocation(window, active_actions)
    active = 0
    for allocation in allocations:
        expected = active_counts.get(allocation.id, 0)
        if allocation.active_claim_count != expected:
            allocation.active_claim_count = expected
            allocation.version += 1
        active += expected
    if window.active_claim_count != active:
        window.active_claim_count = active
        window.version += 1
    return max(0, previous_active - active)


def sync_window_capacity(window: DispatchClaimWindow, capacity: int) -> None:
    occupied = int(window.active_claim_count) + int(window.effective_unclaimed_count)
    expected = max(occupied, capacity)
    if window.claim_capacity != expected:
        window.claim_capacity = expected
        window.version += 1


def confirm_dispatch_claim(session: Session, action: Action, binding: DispatchClaimBinding) -> bool:
    with session.no_autoflush:
        scope = _locked_scope(session, binding.dispatcher_scope)
        window = _locked_window(session, binding.window_id)
        allocation = _locked_allocation(session, binding.shard_allocation_id)
        reservation = _locked_reservation(session, binding.reservation_id)
    if scope is None or window is None or allocation is None or reservation is None:
        return False
    if not _reservation_matches(reservation, action, binding) or not _claim_available(reservation, allocation, window, scope):
        return False
    _consume_reservation(reservation, allocation, window, scope)
    action.result = {**(action.result or {}), **binding_metadata(binding), "dispatch_claim_active": True}
    return True


def release_dispatch_claim(session: Session, action: Action) -> bool:
    result = action.result if isinstance(action.result, dict) else {}
    if action.status == "executing" or not result.get("dispatch_claim_active"):
        return False
    scope, window, allocation = _locked_release_ledger(session, action, result)
    session.flush([action])
    reconciliation = _reconcile_released_claim(session, scope, window, allocation)
    action.result = _released_claim_result(result, reconciliation)
    return True


def lock_dispatch_claim_prefix(session: Session, action: Action) -> bool:
    result = action.result if isinstance(action.result, dict) else {}
    if not result.get("dispatch_claim_scope"):
        return False
    _locked_release_ledger(session, action, result)
    return True


def _locked_release_ledger(
    session: Session,
    action: Action,
    result: dict,
) -> tuple[DispatchClaimScope, DispatchClaimWindow, DispatchClaimShardAllocation]:
    with session.no_autoflush:
        scope = _locked_scope(session, str(result.get("dispatch_claim_scope") or ""))
        window = _locked_window(session, str(result.get("dispatch_claim_window_id") or ""))
        allocation = _locked_allocation(
            session,
            str(result.get("dispatch_claim_shard_allocation_id") or ""),
        )
        reservation = _locked_reservation(
            session,
            str(result.get("dispatch_reservation_id") or ""),
        )
    if scope is None or window is None or allocation is None or reservation is None:
        raise RuntimeError(f"dispatch claim ledger missing for action {action.id}")
    if not _release_binding_matches(scope, window, allocation, reservation):
        raise RuntimeError(f"dispatch claim ledger binding mismatch for action {action.id}")
    return scope, window, allocation


def _release_binding_matches(
    scope: DispatchClaimScope,
    window: DispatchClaimWindow,
    allocation: DispatchClaimShardAllocation,
    reservation: DispatchClaimReservation,
) -> bool:
    return bool(
        window.dispatcher_scope == scope.dispatcher_scope
        and allocation.dispatch_claim_window_id == window.id
        and reservation.dispatch_claim_shard_allocation_id == allocation.id
    )


def _reconcile_released_claim(
    session: Session,
    scope: DispatchClaimScope,
    window: DispatchClaimWindow,
    allocation: DispatchClaimShardAllocation,
) -> dict[str, object]:
    before = _release_counter_snapshot(scope, window, allocation)
    active_actions = _scope_active_claims(session, scope.dispatcher_scope)
    expected = _release_counter_expectation(window, allocation, active_actions)
    reconcile_scope_active(session, scope)
    reconcile_window_active(window, window_allocations(session, window.id), active_actions)
    after = _release_counter_snapshot(scope, window, allocation)
    return _release_reconciliation_snapshot(before, expected, after)


def _release_counter_snapshot(
    scope: DispatchClaimScope,
    window: DispatchClaimWindow,
    allocation: DispatchClaimShardAllocation,
) -> dict[str, int]:
    return {
        "scope": int(scope.active_claim_count),
        "window": int(window.active_claim_count),
        "allocation": int(allocation.active_claim_count),
    }


def _release_counter_expectation(
    window: DispatchClaimWindow,
    allocation: DispatchClaimShardAllocation,
    active_actions: list[Action],
) -> dict[str, int]:
    allocation_counts = _active_claim_counts_by_allocation(window, active_actions)
    return {
        "scope": len(active_actions),
        "window": sum(allocation_counts.values()),
        "allocation": allocation_counts.get(allocation.id, 0),
    }


def _release_reconciliation_snapshot(
    before: dict[str, int],
    expected: dict[str, int],
    after: dict[str, int],
) -> dict[str, object]:
    drifted = any(before[name] != expected[name] + 1 for name in before)
    return {
        "drifted": drifted,
        **{name: {"before": before[name], "after": after[name]} for name in before},
    }


def _released_claim_result(result: dict, reconciliation: dict[str, object]) -> dict:
    released = {**result, "dispatch_claim_active": False, "dispatch_claim_released_at": _now().isoformat()}
    if reconciliation["drifted"]:
        released["dispatch_claim_release_reconciliation"] = reconciliation
    return released


def task_dispatch_claim_snapshot(session: Session, task: Task) -> dict[str, object]:
    window = _latest_task_claim_window(session, task)
    if window is None:
        return {}
    scope = session.scalar(select(DispatchClaimScope).where(DispatchClaimScope.dispatcher_scope == window.dispatcher_scope))
    rows = _task_window_reservations(session, task, window.id)
    return {
        "dispatcher_scope": window.dispatcher_scope,
        "bucket_start": window.bucket_start.isoformat(),
        "bucket_end": window.bucket_end.isoformat(),
        "claim_capacity": window.claim_capacity,
        "active_claim_count": window.active_claim_count,
        "global_claim_capacity": scope.claim_capacity if scope else window.claim_capacity,
        "global_active_claim_count": scope.active_claim_count if scope else window.active_claim_count,
        "unclaimed_allocated_count": window.unclaimed_allocated_count,
        "effective_unclaimed_count": window.effective_unclaimed_count,
        "dispatch_allocation_epoch": window.allocation_epoch,
        "invariant_ok": bool(
            window.active_claim_count + window.effective_unclaimed_count <= window.claim_capacity
            and (scope is None or scope.active_claim_count <= scope.claim_capacity)
        ),
        "reservations": [_reservation_snapshot(reservation, allocation) for reservation, allocation in rows],
    }


def reservation_available(reservation: DispatchClaimReservation | None) -> int:
    if reservation is None:
        return 0
    return max(
        0,
        int(reservation.reserved_claims)
        - int(reservation.claimed_count)
        - int(reservation.bound_count)
        - int(reservation.released_count),
    )


def for_update(session: Session, statement):
    return statement if not session.bind or session.bind.dialect.name == "sqlite" else statement.with_for_update()


def reservation_key(
    reservation: DispatchClaimReservation,
    allocation: DispatchClaimShardAllocation,
) -> tuple[int, str, str, int, int]:
    return (
        reservation.tenant_id,
        reservation.task_id,
        reservation.claim_class,
        allocation.account_shard_total,
        allocation.account_shard_index,
    )


def _create_scope(session: Session, scope: str, capacity: int) -> DispatchClaimScope:
    ledger = DispatchClaimScope(dispatcher_scope=scope, claim_capacity=capacity)
    try:
        with session.begin_nested():
            session.add(ledger)
            session.flush()
        return ledger
    except IntegrityError:
        statement = select(DispatchClaimScope).where(DispatchClaimScope.dispatcher_scope == scope)
        existing = session.scalar(for_update(session, statement))
        if existing is not None:
            return existing
    raise RuntimeError("unable to create dispatch claim scope")


def _scope_active_claims(session: Session, scope: str) -> list[Action]:
    actions = session.scalars(select(Action).where(Action.status == "executing"))
    return [
        action
        for action in actions
        if _active_claim_result(action).get("dispatch_claim_scope") == scope
    ]


def _active_claim_counts_by_allocation(
    window: DispatchClaimWindow,
    active_actions: list[Action],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for action in active_actions:
        result = _active_claim_result(action)
        if result.get("dispatch_claim_window_id") != window.id:
            continue
        allocation_id = str(result.get("dispatch_claim_shard_allocation_id") or "")
        if allocation_id:
            counts[allocation_id] = counts.get(allocation_id, 0) + 1
    return counts


def _active_claim_result(action: Action) -> dict:
    result = action.result if isinstance(action.result, dict) else {}
    return result if result.get("dispatch_claim_active") else {}


def _create_window(
    session: Session,
    scope: str,
    bucket_start: datetime,
    bucket_end: datetime,
    capacity: int,
) -> DispatchClaimWindow:
    window = DispatchClaimWindow(
        dispatcher_scope=scope,
        bucket_start=bucket_start,
        bucket_end=bucket_end,
        claim_capacity=capacity,
    )
    try:
        with session.begin_nested():
            session.add(window)
            session.flush()
        return window
    except IntegrityError:
        statement = select(DispatchClaimWindow).where(
            DispatchClaimWindow.dispatcher_scope == scope,
            DispatchClaimWindow.bucket_start == bucket_start,
            DispatchClaimWindow.bucket_end == bucket_end,
        )
        existing = session.scalar(for_update(session, statement))
        if existing is not None:
            return existing
    raise RuntimeError("unable to create dispatch claim window")


def _bucket_bounds(now: datetime) -> tuple[datetime, datetime]:
    timestamp = int(now.timestamp())
    bucket_timestamp = timestamp - (timestamp % CLAIM_WINDOW_SECONDS)
    start = datetime.fromtimestamp(bucket_timestamp, tz=now.tzinfo)
    return start, start + timedelta(seconds=CLAIM_WINDOW_SECONDS)


def _latest_task_claim_window(session: Session, task: Task) -> DispatchClaimWindow | None:
    statement = select(DispatchClaimWindow).join(
        DispatchClaimShardAllocation,
        DispatchClaimShardAllocation.dispatch_claim_window_id == DispatchClaimWindow.id,
    ).join(
        DispatchClaimReservation,
        DispatchClaimReservation.dispatch_claim_shard_allocation_id == DispatchClaimShardAllocation.id,
    ).where(
        DispatchClaimReservation.tenant_id == task.tenant_id,
        DispatchClaimReservation.task_id == task.id,
    ).order_by(DispatchClaimWindow.bucket_start.desc(), DispatchClaimWindow.id.desc()).limit(1)
    return session.scalar(statement)


def _task_window_reservations(
    session: Session,
    task: Task,
    window_id: str,
) -> list[tuple[DispatchClaimReservation, DispatchClaimShardAllocation]]:
    window = session.get(DispatchClaimWindow, window_id)
    if window is None:
        return []
    statement = select(DispatchClaimReservation, DispatchClaimShardAllocation).join(
        DispatchClaimShardAllocation,
        DispatchClaimShardAllocation.id == DispatchClaimReservation.dispatch_claim_shard_allocation_id,
    ).where(
        DispatchClaimShardAllocation.dispatch_claim_window_id == window_id,
        DispatchClaimShardAllocation.dispatch_allocation_epoch
        == window.allocation_epoch,
        DispatchClaimReservation.tenant_id == task.tenant_id,
        DispatchClaimReservation.task_id == task.id,
    ).order_by(DispatchClaimReservation.claim_class.asc(), DispatchClaimReservation.id.asc())
    return list(session.execute(statement).all())


def _reservation_snapshot(
    reservation: DispatchClaimReservation,
    allocation: DispatchClaimShardAllocation,
) -> dict[str, object]:
    return {
        "id": reservation.id,
        "claim_class": reservation.claim_class,
        "dispatch_allocation_epoch": reservation.dispatch_allocation_epoch,
        "account_shard_total": allocation.account_shard_total,
        "account_shard_index": allocation.account_shard_index,
        "required_claims": reservation.required_claims,
        "reserved_claims": reservation.reserved_claims,
        "claimed_count": reservation.claimed_count,
        "available_claims": reservation_available(reservation),
        "urgency_score": reservation.urgency_score,
        "reason": reservation.reason,
    }


def _locked_reservation(session: Session, reservation_id: str) -> DispatchClaimReservation | None:
    if not reservation_id:
        return None
    return session.scalar(for_update(session, select(DispatchClaimReservation).where(DispatchClaimReservation.id == reservation_id)))


def _locked_scope(session: Session, scope: str) -> DispatchClaimScope | None:
    if not scope:
        return None
    return session.scalar(for_update(session, select(DispatchClaimScope).where(DispatchClaimScope.dispatcher_scope == scope)))


def _locked_allocation(session: Session, allocation_id: str) -> DispatchClaimShardAllocation | None:
    if not allocation_id:
        return None
    return session.scalar(for_update(session, select(DispatchClaimShardAllocation).where(DispatchClaimShardAllocation.id == allocation_id)))


def _locked_window(session: Session, window_id: str) -> DispatchClaimWindow | None:
    if not window_id:
        return None
    return session.scalar(for_update(session, select(DispatchClaimWindow).where(DispatchClaimWindow.id == window_id)))


def _reservation_matches(
    reservation: DispatchClaimReservation,
    action: Action,
    binding: DispatchClaimBinding,
) -> bool:
    return bool(
        reservation.dispatch_claim_shard_allocation_id == binding.shard_allocation_id
        and reservation.tenant_id == action.tenant_id
        and reservation.task_id == action.task_id
        and reservation.claim_class == binding.claim_class
    )


def _claim_available(
    reservation: DispatchClaimReservation,
    allocation: DispatchClaimShardAllocation,
    window: DispatchClaimWindow,
    scope: DispatchClaimScope,
) -> bool:
    return bool(
        reservation.claimed_count < reservation.reserved_claims
        and allocation.unclaimed_allocated_count > 0
        and scope.active_claim_count < scope.claim_capacity
        and window.active_claim_count + window.effective_unclaimed_count <= window.claim_capacity
    )


def _consume_reservation(
    reservation: DispatchClaimReservation,
    allocation: DispatchClaimShardAllocation,
    window: DispatchClaimWindow,
    scope: DispatchClaimScope,
) -> None:
    reservation.claimed_count += 1
    reservation.version += 1
    allocation.unclaimed_allocated_count -= 1
    allocation.active_claim_count += 1
    allocation.version += 1
    window.unclaimed_allocated_count -= 1
    window.effective_unclaimed_count -= 1
    window.active_claim_count += 1
    window.version += 1
    scope.active_claim_count += 1
    scope.version += 1
