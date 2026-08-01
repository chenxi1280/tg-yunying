from __future__ import annotations

from collections import Counter
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    DispatchClaimScope,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
)
from app.services._common import _now

from .dispatch_activation_ledger import validate_live_window_ledger
from .dispatch_runtime_contract import (
    DispatchRuntimeContractError,
    build_dispatch_runtime_contract,
)


def validate_dispatch_ledgers_for_runtime(
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
    active_actions = _active_scope_actions(session, contract.dispatcher_scope)
    if scope is None or scope.active_claim_count != len(active_actions):
        raise _invariant_error("scope_active_projection")
    if scope.active_claim_count > contract.scope_capacity:
        raise _invariant_error("scope_capacity")
    rows = _runtime_validation_windows(
        session,
        contract.dispatcher_scope,
        active_actions,
        observed_at,
    )
    _validate_window_bindings(active_actions, rows)
    for window, is_live in rows:
        _validate_runtime_window(
            session,
            window,
            active_actions,
            is_live=bool(is_live),
            capacity=contract.scope_capacity,
        )


def _runtime_validation_windows(
    session: Session,
    scope: str,
    active_actions: list[Action],
    observed_at: datetime,
) -> list[tuple[DispatchClaimWindow, bool]]:
    active_allocation_windows = select(
        DispatchClaimShardAllocation.dispatch_claim_window_id,
    ).where(DispatchClaimShardAllocation.active_claim_count != 0)
    bound_window_ids = tuple(_active_action_window_ids(active_actions))
    is_live = DispatchClaimWindow.bucket_end > observed_at
    statement = select(DispatchClaimWindow, is_live.label("is_live")).where(
        DispatchClaimWindow.dispatcher_scope == scope,
        or_(
            is_live,
            DispatchClaimWindow.active_claim_count != 0,
            DispatchClaimWindow.id.in_(active_allocation_windows),
            DispatchClaimWindow.id.in_(bound_window_ids),
        ),
    )
    return [(window, bool(live)) for window, live in session.execute(statement)]


def _validate_window_bindings(
    active_actions: list[Action],
    rows: list[tuple[DispatchClaimWindow, bool]],
) -> None:
    if any(not _active_action_binding(action)[0] for action in active_actions):
        raise _invariant_error("runtime_window_binding")
    known_ids = {window.id for window, _ in rows}
    if not _active_action_window_ids(active_actions).issubset(known_ids):
        raise _invariant_error("runtime_window_binding")


def _validate_runtime_window(
    session: Session,
    window: DispatchClaimWindow,
    active_actions: list[Action],
    *,
    is_live: bool,
    capacity: int,
) -> None:
    allocations = list(session.scalars(select(
        DispatchClaimShardAllocation,
    ).where(
        DispatchClaimShardAllocation.dispatch_claim_window_id == window.id,
    )))
    expected = _expected_allocation_active(window.id, active_actions)
    if not set(expected).issubset({row.id for row in allocations}):
        raise _invariant_error("runtime_allocation_binding")
    if window.active_claim_count != sum(expected.values()):
        raise _invariant_error("runtime_active_projection")
    if any(row.active_claim_count != expected.get(row.id, 0) for row in allocations):
        raise _invariant_error("runtime_active_projection")
    if is_live:
        validate_live_window_ledger(
            session,
            window,
            capacity,
            allocations=allocations,
        )
    elif window.effective_unclaimed_count != 0:
        raise _invariant_error("closed_window_effective_unclaimed")


def _active_scope_actions(session: Session, scope: str) -> list[Action]:
    rows = session.scalars(select(Action).where(Action.status == "executing"))
    return [
        action for action in rows
        if isinstance(action.result, dict)
        and action.result.get("dispatch_claim_active") is True
        and action.result.get("dispatch_claim_scope") == scope
    ]


def _active_action_window_ids(active_actions: list[Action]) -> set[str]:
    return {_active_action_binding(action)[0] for action in active_actions} - {""}


def _active_action_binding(action: Action) -> tuple[str, str]:
    result = action.result if isinstance(action.result, dict) else {}
    return (
        str(result.get("dispatch_claim_window_id") or ""),
        str(result.get("dispatch_claim_shard_allocation_id") or ""),
    )


def _expected_allocation_active(
    window_id: str,
    active_actions: list[Action],
) -> Counter:
    expected: Counter = Counter()
    for action in active_actions:
        action_window_id, allocation_id = _active_action_binding(action)
        if action_window_id != window_id:
            continue
        if not allocation_id:
            raise _invariant_error("runtime_allocation_binding")
        expected[allocation_id] += 1
    return expected


def _invariant_error(detail: str) -> DispatchRuntimeContractError:
    return DispatchRuntimeContractError(
        "dispatch_ledger_invariant_failed",
        detail,
    )


__all__ = ["validate_dispatch_ledgers_for_runtime"]
