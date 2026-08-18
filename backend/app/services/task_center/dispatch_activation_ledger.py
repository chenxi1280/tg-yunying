from __future__ import annotations

from collections import Counter
from datetime import datetime
import json

from sqlalchemy import and_, func, or_, select
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
        or_(
            Action.result["dispatch_claim_active"].as_boolean().is_(True),
            and_(
                Action.task_type == "search_click",
                Action.action_type == "search_join",
            ),
        ),
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
    live_windows, closed_windows, allocations_by_window = (
        _locked_activation_windows(
            session,
            contract.dispatcher_scope,
            observed_at,
        )
    )
    released = _reconcile_live_windows(
        session,
        windows=live_windows,
        allocations_by_window=allocations_by_window,
        active_actions=active_actions,
        observed_at=observed_at,
        contract=contract,
    )
    _reconcile_closed_active_windows(
        closed_windows,
        allocations_by_window,
        active_actions,
    )
    reconcile_scope_active(session, scope)
    session.flush()
    validate_dispatch_ledgers_for_activation(
        session,
        settings,
        now=observed_at,
    )
    return {
        "scope_id": scope.id,
        "window_count": len(live_windows),
        "closed_active_window_count": len(closed_windows),
        "active_claim_count": scope.active_claim_count,
        "released_unclaimed_count": released,
    }


def _reconcile_live_windows(
    session: Session,
    *,
    windows: list[DispatchClaimWindow],
    allocations_by_window: dict[str, list[DispatchClaimShardAllocation]],
    active_actions: list[Action],
    observed_at: datetime,
    contract,
) -> int:
    released = 0
    for window in windows:
        allocations = allocations_by_window.get(window.id, [])
        released += _reconcile_window_epochs(
            session,
            window,
            allocations=allocations,
            now=observed_at,
            contract_version=contract.rebuild_contract_version,
            shard_total=contract.runtime_shard_total,
        )
        reconcile_window_active(window, allocations, active_actions)
        sync_window_unclaimed_total(window, allocations)
        sync_window_capacity(window, contract.scope_capacity)
    return released


def _reconcile_closed_active_windows(
    windows: list[DispatchClaimWindow],
    allocations_by_window: dict[str, list[DispatchClaimShardAllocation]],
    active_actions: list[Action],
) -> None:
    for window in windows:
        reconcile_window_active(
            window,
            allocations_by_window.get(window.id, []),
            active_actions,
        )


def validate_dispatch_ledgers_for_activation(
    session: Session,
    settings,
    *,
    now: datetime | None = None,
) -> None:
    contract = build_dispatch_runtime_contract(settings)
    observed_at = now or _now()
    scope = session.scalar(select(DispatchClaimScope).where(
        DispatchClaimScope.dispatcher_scope == contract.dispatcher_scope,
    ))
    if scope is None or scope.active_claim_count > contract.scope_capacity:
        raise _invariant_error("scope_capacity")
    windows = list(session.scalars(select(DispatchClaimWindow).where(
        DispatchClaimWindow.dispatcher_scope == contract.dispatcher_scope,
        DispatchClaimWindow.bucket_end > observed_at,
    )))
    for window in windows:
        validate_live_window_ledger(session, window, contract.scope_capacity)
    if _closed_active_drift_count(
        session,
        contract.dispatcher_scope,
        observed_at,
    ):
        raise _invariant_error("closed_window_active")


def _recover_fenced_action(
    session: Session,
    action: Action,
    actor: str,
) -> None:
    from .dispatcher import (
        _finalize_dispatch_action,
        _mark_unknown_after_send,
    )

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


def _locked_activation_windows(
    session: Session,
    scope: str,
    observed_at: datetime,
) -> tuple[
    list[DispatchClaimWindow],
    list[DispatchClaimWindow],
    dict[str, list[DispatchClaimShardAllocation]],
]:
    active_allocation_windows = select(
        DispatchClaimShardAllocation.dispatch_claim_window_id,
    ).where(DispatchClaimShardAllocation.active_claim_count != 0)
    closed_active = and_(
        DispatchClaimWindow.bucket_end <= observed_at,
        or_(
            DispatchClaimWindow.active_claim_count != 0,
            DispatchClaimWindow.id.in_(active_allocation_windows),
        ),
    )
    is_live = DispatchClaimWindow.bucket_end > observed_at
    statement = select(DispatchClaimWindow, is_live.label("is_live")).where(
        DispatchClaimWindow.dispatcher_scope == scope,
        or_(is_live, closed_active),
    ).order_by(DispatchClaimWindow.bucket_start.asc(), DispatchClaimWindow.id.asc())
    rows = session.execute(for_update(session, statement)).all()
    windows = [window for window, _ in rows]
    live_windows = [window for window, live in rows if live]
    closed_windows = [window for window, live in rows if not live]
    allocations = _locked_allocations_by_window(
        session,
        windows,
    )
    return live_windows, closed_windows, allocations


def _locked_allocations_by_window(
    session: Session,
    windows: list[DispatchClaimWindow],
) -> dict[str, list[DispatchClaimShardAllocation]]:
    result = {window.id: [] for window in windows}
    if not result:
        return result
    statement = select(DispatchClaimShardAllocation).where(
        DispatchClaimShardAllocation.dispatch_claim_window_id.in_(tuple(result)),
    ).order_by(
        DispatchClaimShardAllocation.dispatch_claim_window_id.asc(),
        DispatchClaimShardAllocation.dispatch_allocation_epoch.asc(),
        DispatchClaimShardAllocation.id.asc(),
    )
    for allocation in session.scalars(for_update(session, statement)):
        result[allocation.dispatch_claim_window_id].append(allocation)
    return result


def _closed_active_drift_count(
    session: Session,
    scope: str,
    observed_at: datetime,
) -> int:
    active_allocation_windows = select(
        DispatchClaimShardAllocation.dispatch_claim_window_id,
    ).where(DispatchClaimShardAllocation.active_claim_count != 0)
    count = session.scalar(select(func.count(DispatchClaimWindow.id)).where(
        DispatchClaimWindow.dispatcher_scope == scope,
        DispatchClaimWindow.bucket_end <= observed_at,
        or_(
            DispatchClaimWindow.active_claim_count != 0,
            DispatchClaimWindow.id.in_(active_allocation_windows),
        ),
    ))
    return int(count or 0)


def _reconcile_window_epochs(
    session: Session,
    window: DispatchClaimWindow,
    *,
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


def validate_live_window_ledger(
    session: Session,
    window: DispatchClaimWindow,
    capacity: int,
    *,
    allocations: list[DispatchClaimShardAllocation] | None = None,
) -> None:
    allocations = allocations if allocations is not None else list(session.scalars(
        select(DispatchClaimShardAllocation).where(
            DispatchClaimShardAllocation.dispatch_claim_window_id == window.id,
        ),
    ))
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
    "validate_live_window_ledger",
]
