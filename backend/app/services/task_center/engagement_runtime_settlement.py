from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountBehaviorBudgetLedger,
    AccountBehaviorBudgetReservation,
    AccountPoolConcurrencyLease,
    Action,
    ExecutionAttempt,
    RemoteInvocationFence,
)
from app.services._common import _now

from .engagement_runtime_circuit import (
    lock_resilience_policy,
    record_confirmed,
    record_failed,
    record_unknown,
)
from .engagement_recovery_outcome import OUTCOME_UNPROVEN

SETTLEABLE_ATTEMPT_STATES = frozenset({
    "success", "failed", "result_unknown", "skipped_before_gateway", "call_not_started",
})
UNSTARTED_ATTEMPT_STATES = frozenset({"before_call", "before_gateway"})
PRECALL_TERMINAL_ACTION_STATES = frozenset({"failed", "retryable_failed", "cancelled", "stopped", "skipped"})


def attempt_resources(session: Session, attempt_id: str):
    lease = session.scalar(select(AccountPoolConcurrencyLease)
        .where(AccountPoolConcurrencyLease.attempt_id == attempt_id)
        .with_for_update().execution_options(populate_existing=True))
    reservation = session.scalar(select(AccountBehaviorBudgetReservation)
        .where(AccountBehaviorBudgetReservation.attempt_id == attempt_id)
        .with_for_update().execution_options(populate_existing=True))
    fence = session.scalar(select(RemoteInvocationFence)
        .where(RemoteInvocationFence.attempt_id == attempt_id)
        .with_for_update().execution_options(populate_existing=True))
    if lease is None and reservation is None and fence is None:
        return None, None, None
    if lease is None or reservation is None or fence is None:
        raise RuntimeError("engagement_runtime_resource_set_incomplete")
    return lease, reservation, fence


def settle_resource_set(
    session: Session,
    attempt: ExecutionAttempt,
    action: Action,
    *,
    lease: AccountPoolConcurrencyLease,
    reservation: AccountBehaviorBudgetReservation,
    fence: RemoteInvocationFence,
    remote_mutation_started: bool | None,
) -> None:
    if not _settlement_ready(attempt, action, lease=lease, reservation=reservation, fence=fence):
        return
    if _already_settled(
        attempt,
        lease=lease,
        reservation=reservation,
        fence=fence,
    ):
        return
    lock_resilience_policy(session, fence.resilience_policy_revision_id)
    if attempt.status == "result_unknown":
        _settle_unknown(
            session,
            attempt,
            lease=lease,
            reservation=reservation,
            fence=fence,
        )
        return
    if attempt.status == "success":
        _settle_confirmed(
            session,
            lease=lease,
            reservation=reservation,
            fence=fence,
        )
        return
    _settle_failed(
        session,
        attempt,
        lease=lease,
        reservation=reservation,
        fence=fence,
        remote_mutation_started=remote_mutation_started,
    )


def _settlement_ready(attempt, action, *, lease, reservation, fence):
    if attempt.status in SETTLEABLE_ATTEMPT_STATES:
        return True
    if (attempt.status not in UNSTARTED_ATTEMPT_STATES
            or action.status not in PRECALL_TERMINAL_ACTION_STATES
            or attempt.gateway_call_started_at is not None
            or fence.started_at is not None
            or (lease.state, reservation.state, fence.state) != ("reserved", "reserved", "reserved")):
        return False
    attempt.status = "skipped_before_gateway"
    attempt.after_call_at = _now()
    attempt.failure_type = "action_terminal_before_gateway"
    return True


def _already_settled(
    attempt: ExecutionAttempt,
    *,
    lease: AccountPoolConcurrencyLease,
    reservation: AccountBehaviorBudgetReservation,
    fence: RemoteInvocationFence,
) -> bool:
    if attempt.status == "result_unknown":
        expected_lease_state = (
            "released"
            if fence.transport_termination_state == "acknowledged"
            else "remote_unknown"
        )
        return (
            lease.state == expected_lease_state
            and reservation.state == "unknown"
            and fence.state == "remote_unknown"
            and fence.transport_termination_state == _termination_state(attempt, fence)
        )
    if attempt.status == "success":
        return (
            lease.state == "released"
            and reservation.state == "confirmed"
            and fence.business_outcome_state == "confirmed"
        )
    return (
        lease.state == "released"
        and reservation.state == ("confirmed" if fence.business_outcome_state == "failed" else "released")
        and fence.state == "terminal"
    )


def move_counter(
    ledger: AccountBehaviorBudgetLedger,
    action_class: str,
    *,
    old_state: str | None,
    new_state: str | None,
) -> None:
    counters = dict(ledger.counters or {})
    states = dict(counters.get(action_class) or {})
    if old_state:
        states[old_state] = max(0, int(states.get(old_state) or 0) - 1)
    if new_state:
        states[new_state] = int(states.get(new_state) or 0) + 1
    counters[action_class] = states
    ledger.counters = counters
    ledger.version = int(ledger.version or 0) + 1


def locked_ledger_by_id(
    session: Session,
    ledger_id: str,
) -> AccountBehaviorBudgetLedger:
    ledger = session.scalar(
        select(AccountBehaviorBudgetLedger)
        .where(AccountBehaviorBudgetLedger.id == ledger_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if ledger is None:
        raise RuntimeError("account_behavior_budget_ledger_missing")
    return ledger


def _settle_unknown(
    session: Session,
    attempt: ExecutionAttempt,
    *,
    lease: AccountPoolConcurrencyLease,
    reservation: AccountBehaviorBudgetReservation,
    fence: RemoteInvocationFence,
) -> None:
    termination_state = _termination_state(attempt, fence)
    _settle_unknown_lease(lease, termination_state)
    _transition_reservation(session, reservation, "unknown")
    reservation.settled_at = _now()
    fence.state = "remote_unknown"
    fence.transport_termination_state = termination_state
    fence.cancellation_requested_at = (
        _now() if termination_state == "cancellation_unconfirmed" else None
    )
    fence.transport_terminated_at = (
        _now() if termination_state == "acknowledged" else None
    )
    fence.business_outcome_state = "unknown"
    fence.terminal_at = None
    record_unknown(
        session,
        lease,
        fence,
        failure_code=attempt.failure_type or "remote_result_unknown",
        failure_detail=attempt.failure_detail or "",
    )


def _termination_state(attempt, fence):
    if fence.transport_termination_state == "acknowledged":
        return "acknowledged"
    return str((attempt.result_snapshot or {}).get("transport_termination_state") or "unproven")


def _settle_unknown_lease(
    lease: AccountPoolConcurrencyLease,
    termination_state: str,
) -> None:
    if termination_state == "acknowledged":
        lease.state = "released"
        lease.released_at = _now()
        lease.release_reason = "transport_terminated_business_unknown"
        return
    lease.state = "remote_unknown"
    lease.released_at = None
    lease.release_reason = ""


def _settle_confirmed(
    session: Session,
    *,
    lease: AccountPoolConcurrencyLease,
    reservation: AccountBehaviorBudgetReservation,
    fence: RemoteInvocationFence,
) -> None:
    lease.state = "released"
    lease.released_at = _now()
    lease.release_reason = "remote_confirmed"
    _transition_reservation(session, reservation, "confirmed")
    reservation.settled_at = _now()
    fence.state = "terminal"
    fence.transport_termination_state = "acknowledged"
    fence.transport_terminated_at = _now()
    fence.business_outcome_state = "confirmed"
    fence.terminal_at = _now()
    record_confirmed(session, lease, fence)


def _settle_failed(
    session: Session,
    attempt: ExecutionAttempt,
    *,
    lease: AccountPoolConcurrencyLease,
    reservation: AccountBehaviorBudgetReservation,
    fence: RemoteInvocationFence,
    remote_mutation_started: bool | None,
) -> None:
    if (type(remote_mutation_started) is not bool
            and (attempt.gateway_call_started_at is not None or fence.started_at is not None)):
        raise RuntimeError(OUTCOME_UNPROVEN)
    lease.state = "released"
    lease.released_at = _now()
    lease.release_reason = "remote_terminal_failed"
    # Confirmed budget consumption is independent of the failed business result.
    budget_state = "confirmed" if remote_mutation_started is True else "released"
    _transition_reservation(session, reservation, budget_state)
    reservation.settled_at = _now()
    fence.state = "terminal"
    fence.transport_termination_state = "acknowledged"
    fence.transport_terminated_at = _now()
    fence.business_outcome_state = (
        "failed" if remote_mutation_started is True else "safely_not_called"
    )
    fence.terminal_at = _now()
    record_failed(
        session,
        lease,
        fence,
        failure_type=str(attempt.failure_type or ""),
        failure_detail=str(attempt.failure_detail or ""),
        remote_mutation_started=remote_mutation_started,
    )


def _transition_reservation(
    session: Session,
    reservation: AccountBehaviorBudgetReservation,
    new_state: str,
) -> None:
    old_state = reservation.state
    if old_state == new_state:
        return
    counter_states = {"reserved", "call_issued", "unknown", "confirmed", "unowned"}
    old_counter_state = old_state if old_state in counter_states else None
    new_counter_state = new_state if new_state in counter_states else None
    ledger = locked_ledger_by_id(session, reservation.ledger_id)
    move_counter(
        ledger,
        reservation.action_class,
        old_state=old_counter_state,
        new_state=new_counter_state,
    )
    reservation.state = new_state


__all__ = ["locked_ledger_by_id", "move_counter", "settle_resource_set"]
