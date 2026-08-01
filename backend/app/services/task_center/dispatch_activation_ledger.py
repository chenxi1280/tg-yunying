from __future__ import annotations

from collections import Counter
from datetime import datetime
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AuditLog,
    DispatchClaimReservation,
    DispatchClaimScope,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    ExecutionAttempt,
)
from app.services._common import _now

from .dispatch_claim_ledger import (
    for_update,
    reconcile_scope_active,
    reconcile_window_active,
    scope_for_update,
    sync_window_capacity,
    window_allocations,
    window_reservations,
)
from .dispatch_claim_reconciliation import (
    reconcile_window_unclaimed,
    sync_window_unclaimed_total,
)
from .dispatch_runtime_contract import (
    DispatchRuntimeContractError,
    build_dispatch_runtime_contract,
)


def recover_fenced_dispatch_actions(
    session: Session,
    *,
    actor: str,
    limit: int = 100,
) -> int:
    actions = list(session.scalars(for_update(session, select(Action).where(
        Action.status == "executing",
        Action.result["dispatch_claim_active"].as_boolean().is_(True),
    ).order_by(Action.id.asc()).limit(max(1, limit)))))
    for action in actions:
        _recover_fenced_action(session, action, actor)
    return len(actions)


def reconcile_dispatch_ledgers_for_activation(
    session: Session,
    settings,
    *,
    now: datetime | None = None,
) -> dict:
    contract = build_dispatch_runtime_contract(settings)
    observed_at = now or _now()
    scope = scope_for_update(
        session,
        contract.dispatcher_scope,
        contract.scope_capacity,
    )
    active_actions = _active_scope_actions(session, contract.dispatcher_scope)
    windows = _locked_windows(session, contract.dispatcher_scope)
    released = 0
    for window in windows:
        allocations = window_allocations(session, window.id)
        released += _reconcile_window_epochs(
            session,
            window,
            allocations,
            observed_at,
            contract.rebuild_contract_version,
            contract.runtime_shard_total,
        )
        reconcile_window_active(window, allocations, active_actions)
        sync_window_unclaimed_total(window, allocations)
        sync_window_capacity(window, contract.scope_capacity)
    reconcile_scope_active(session, scope)
    validate_dispatch_ledgers_for_activation(session, settings)
    return {
        "scope_id": scope.id,
        "window_count": len(windows),
        "active_claim_count": scope.active_claim_count,
        "released_unclaimed_count": released,
    }


def validate_dispatch_ledgers_for_activation(session: Session, settings) -> None:
    contract = build_dispatch_runtime_contract(settings)
    scope = session.scalar(select(DispatchClaimScope).where(
        DispatchClaimScope.dispatcher_scope == contract.dispatcher_scope,
    ))
    if scope is None or scope.active_claim_count > contract.scope_capacity:
        raise _invariant_error("scope_capacity")
    windows = list(session.scalars(select(DispatchClaimWindow).where(
        DispatchClaimWindow.dispatcher_scope == contract.dispatcher_scope,
    )))
    for window in windows:
        _validate_window(session, window, contract.scope_capacity)


def _recover_fenced_action(
    session: Session,
    action: Action,
    actor: str,
) -> None:
    from .dispatcher import _finalize_dispatch_action, _mark_unknown_after_send

    attempt = _latest_attempt(session, action.id)
    gateway_started = bool(attempt and attempt.gateway_call_started_at)
    if gateway_started:
        _mark_unknown_after_send(
            session,
            action,
            "发布fence接管已进入Gateway边界的遗留Action",
        )
    else:
        _restore_pre_gateway_action(action, attempt)
    _finalize_dispatch_action(
        session,
        action,
        project_task_stats=False,
    )
    session.add(AuditLog(
        tenant_id=action.tenant_id,
        actor=actor[:100],
        action="发布fence接管遗留dispatch claim",
        target_type="action",
        target_id=action.id,
        detail=json.dumps({
            "gateway_started": gateway_started,
            "outcome": "remote_reconcile" if gateway_started else "pending_reclaim",
        }, ensure_ascii=False, sort_keys=True),
    ))


def _restore_pre_gateway_action(
    action: Action,
    attempt: ExecutionAttempt | None,
) -> None:
    action.status = "pending"
    action.executed_at = None
    action.lease_owner = ""
    action.lease_expires_at = None
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.result = {
        **dict(action.result or {}),
        "dispatch_fence_recovered": True,
        "error_code": "dispatch_fence_pre_gateway_recovered",
    }
    if attempt is not None:
        attempt.status = "call_not_started"
        attempt.after_call_at = _now()
        attempt.result_snapshot = dict(action.result)


def _latest_attempt(session: Session, action_id: str) -> ExecutionAttempt | None:
    return session.scalar(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id == action_id,
    ).order_by(ExecutionAttempt.attempt_no.desc()).limit(1))


def _active_scope_actions(session: Session, scope: str) -> list[Action]:
    rows = session.scalars(select(Action).where(Action.status == "executing"))
    return [
        action for action in rows
        if (action.result or {}).get("dispatch_claim_active") is True
        and (action.result or {}).get("dispatch_claim_scope") == scope
    ]


def _locked_windows(session: Session, scope: str) -> list[DispatchClaimWindow]:
    statement = select(DispatchClaimWindow).where(
        DispatchClaimWindow.dispatcher_scope == scope,
    ).order_by(DispatchClaimWindow.bucket_start.asc(), DispatchClaimWindow.id.asc())
    return list(session.scalars(for_update(session, statement)))


def _reconcile_window_epochs(
    session: Session,
    window: DispatchClaimWindow,
    allocations: list[DispatchClaimShardAllocation],
    now: datetime,
    contract_version: str,
    shard_total: int,
) -> int:
    epochs = sorted({row.dispatch_allocation_epoch for row in allocations})
    released = 0
    for epoch in epochs:
        epoch_allocations = [
            row for row in allocations
            if row.dispatch_allocation_epoch == epoch
        ]
        reservations = window_reservations(
            session,
            window.id,
            allocation_epoch=epoch,
        )
        released += reconcile_window_unclaimed(
            session,
            window,
            allocations=epoch_allocations,
            reservations=reservations,
            now=now,
            current_contract_version=contract_version,
            runtime_shard_total=shard_total,
        )
    return released


def _validate_window(
    session: Session,
    window: DispatchClaimWindow,
    capacity: int,
) -> None:
    allocations = list(session.scalars(select(
        DispatchClaimShardAllocation,
    ).where(
        DispatchClaimShardAllocation.dispatch_claim_window_id == window.id,
    )))
    allocation_ids = [row.id for row in allocations]
    reservations = list(session.scalars(select(DispatchClaimReservation).where(
        DispatchClaimReservation.dispatch_claim_shard_allocation_id.in_(
            allocation_ids,
        ),
    ))) if allocation_ids else []
    unclaimed = Counter({row.id: 0 for row in allocations})
    for reservation in reservations:
        value = _reservation_unclaimed(reservation)
        unclaimed[reservation.dispatch_claim_shard_allocation_id] += value
    if any(row.unclaimed_allocated_count != unclaimed[row.id] for row in allocations):
        raise _invariant_error("allocation_unclaimed")
    effective = sum(unclaimed.values())
    active = sum(int(row.active_claim_count) for row in allocations)
    if window.effective_unclaimed_count != effective:
        raise _invariant_error("window_effective_unclaimed")
    if window.active_claim_count != active or active + effective > capacity:
        raise _invariant_error("window_capacity")


def _reservation_unclaimed(reservation: DispatchClaimReservation) -> int:
    values = (
        int(reservation.reserved_claims),
        int(reservation.claimed_count),
        int(reservation.bound_count),
        int(reservation.released_count),
    )
    reserved, claimed, bound, released = values
    unclaimed = reserved - claimed - released
    if min(values) < 0 or unclaimed < bound:
        raise _invariant_error("reservation_counter")
    return unclaimed


def _invariant_error(detail: str) -> DispatchRuntimeContractError:
    return DispatchRuntimeContractError(
        "dispatch_ledger_invariant_failed",
        detail,
    )


__all__ = [
    "reconcile_dispatch_ledgers_for_activation",
    "recover_fenced_dispatch_actions",
    "validate_dispatch_ledgers_for_activation",
]
