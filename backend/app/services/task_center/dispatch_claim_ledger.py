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

from .dispatch_claim_types import CLAIM_WINDOW_SECONDS, DEFAULT_DISPATCHER_SCOPE, DispatchClaimBinding


def dispatcher_scope(settings) -> str:
    value = str(getattr(settings, "dispatcher_claim_scope", DEFAULT_DISPATCHER_SCOPE) or "").strip()
    return value or DEFAULT_DISPATCHER_SCOPE


def dispatcher_claim_capacity(settings, requested_limit: int) -> int:
    configured_limit = max(1, int(getattr(settings, "action_claim_limit", requested_limit) or requested_limit))
    configured_concurrency = max(1, int(getattr(settings, "dispatcher_concurrency", configured_limit) or configured_limit))
    return min(configured_limit, configured_concurrency)


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


def window_reservations(
    session: Session,
    window_id: str,
) -> dict[tuple[int, str, str, int, int], DispatchClaimReservation]:
    statement = select(DispatchClaimReservation, DispatchClaimShardAllocation).join(
        DispatchClaimShardAllocation,
        DispatchClaimShardAllocation.id == DispatchClaimReservation.dispatch_claim_shard_allocation_id,
    ).where(DispatchClaimShardAllocation.dispatch_claim_window_id == window_id)
    rows = session.execute(for_update(session, statement)).all()
    return {reservation_key(reservation, allocation): reservation for reservation, allocation in rows}


def reconcile_window_active(
    window: DispatchClaimWindow,
    allocations: list[DispatchClaimShardAllocation],
    active_actions: list[Action],
) -> None:
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


def sync_window_capacity(window: DispatchClaimWindow, capacity: int) -> None:
    occupied = int(window.active_claim_count) + int(window.unclaimed_allocated_count)
    expected = max(occupied, capacity)
    if window.claim_capacity != expected:
        window.claim_capacity = expected
        window.version += 1


def confirm_dispatch_claim(session: Session, action: Action, binding: DispatchClaimBinding) -> bool:
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
    scope = _locked_scope(session, str(result.get("dispatch_claim_scope") or ""))
    window = _locked_window(session, str(result.get("dispatch_claim_window_id") or ""))
    allocation = _locked_allocation(session, str(result.get("dispatch_claim_shard_allocation_id") or ""))
    reservation = _locked_reservation(session, str(result.get("dispatch_reservation_id") or ""))
    if scope is None or reservation is None or allocation is None or window is None:
        raise RuntimeError(f"dispatch claim ledger missing for action {action.id}")
    if scope.active_claim_count <= 0 or allocation.active_claim_count <= 0 or window.active_claim_count <= 0:
        raise RuntimeError(f"dispatch claim ledger underflow for action {action.id}")
    scope.active_claim_count -= 1
    scope.version += 1
    allocation.active_claim_count -= 1
    allocation.version += 1
    window.active_claim_count -= 1
    window.version += 1
    action.result = {**result, "dispatch_claim_active": False, "dispatch_claim_released_at": _now().isoformat()}
    return True


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
        "allocation_epoch": window.allocation_epoch,
        "invariant_ok": bool(
            window.active_claim_count + window.unclaimed_allocated_count <= window.claim_capacity
            and (scope is None or scope.active_claim_count <= scope.claim_capacity)
        ),
        "reservations": [_reservation_snapshot(reservation, allocation) for reservation, allocation in rows],
    }


def reservation_available(reservation: DispatchClaimReservation | None) -> int:
    if reservation is None:
        return 0
    return max(0, int(reservation.reserved_claims) - int(reservation.claimed_count))


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


def binding_metadata(binding: DispatchClaimBinding) -> dict[str, object]:
    return {
        "dispatch_claim_class": binding.claim_class,
        "dispatch_reservation_id": binding.reservation_id,
        "dispatch_claim_window_id": binding.window_id,
        "dispatch_claim_shard_allocation_id": binding.shard_allocation_id,
        "dispatch_claim_scope": binding.dispatcher_scope,
        "dispatch_claim_shard": {"total": binding.shard_total, "index": binding.shard_index},
        "dispatch_allocation_epoch": binding.allocation_epoch,
        "dispatch_reservation_reason": binding.reservation_reason,
        "dispatch_urgency_score": binding.urgency_score,
        "dispatch_unserved_strict_classes": list(binding.unserved_strict_classes),
    }


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
    statement = select(DispatchClaimReservation, DispatchClaimShardAllocation).join(
        DispatchClaimShardAllocation,
        DispatchClaimShardAllocation.id == DispatchClaimReservation.dispatch_claim_shard_allocation_id,
    ).where(
        DispatchClaimShardAllocation.dispatch_claim_window_id == window_id,
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
        and window.active_claim_count + window.unclaimed_allocated_count <= window.claim_capacity
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
    window.active_claim_count += 1
    window.version += 1
    scope.active_claim_count += 1
    scope.version += 1
