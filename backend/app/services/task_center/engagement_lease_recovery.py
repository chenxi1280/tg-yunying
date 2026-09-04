"""Recover persisted outcomes, never infer a remote result from a lease's age."""
import logging

from sqlalchemy import and_, case, func, or_, select

from app.models import (Action, ExecutionAttempt, AccountPoolConcurrencyLease,
                        AccountBehaviorBudgetReservation, RemoteInvocationFence)
from .engagement_runtime_settlement import (
    SETTLEABLE_ATTEMPT_STATES, UNSTARTED_ATTEMPT_STATES, PRECALL_TERMINAL_ACTION_STATES,
)

logger = logging.getLogger(__name__)


def _pending_outcomes():
    lease, reservation, fence = AccountPoolConcurrencyLease, AccountBehaviorBudgetReservation, RemoteInvocationFence
    transport = case((fence.transport_termination_state == "acknowledged", "acknowledged"), else_=
        func.coalesce(ExecutionAttempt.result_snapshot["transport_termination_state"].as_string(), "unproven"))
    expected_lease = case((transport == "acknowledged", "released"), else_="remote_unknown")
    return or_(
        ExecutionAttempt.status != "result_unknown",
        lease.state != expected_lease,
        reservation.state != "unknown",
        fence.state != "remote_unknown",
        fence.transport_termination_state != transport,
    )


def _candidates(limit):
    return (select(ExecutionAttempt.id, ExecutionAttempt.action_id)
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .join(AccountPoolConcurrencyLease, AccountPoolConcurrencyLease.attempt_id == ExecutionAttempt.id)
        .join(AccountBehaviorBudgetReservation, AccountBehaviorBudgetReservation.attempt_id == ExecutionAttempt.id)
        .join(RemoteInvocationFence, RemoteInvocationFence.attempt_id == ExecutionAttempt.id)
        .where(AccountPoolConcurrencyLease.state.in_(("reserved", "call_issued", "remote_unknown")),
               or_(ExecutionAttempt.status.in_(SETTLEABLE_ATTEMPT_STATES), _unissued_terminal()), _pending_outcomes())
        .order_by(AccountPoolConcurrencyLease.id).limit(limit)
        .with_for_update(of=Action, skip_locked=True))


def _unissued_terminal():
    return and_(ExecutionAttempt.status.in_(UNSTARTED_ATTEMPT_STATES),
        Action.status.in_(PRECALL_TERMINAL_ACTION_STATES), ExecutionAttempt.gateway_call_started_at.is_(None),
        RemoteInvocationFence.started_at.is_(None), RemoteInvocationFence.state == "reserved",
        AccountPoolConcurrencyLease.state == "reserved", AccountBehaviorBudgetReservation.state == "reserved")


def recover_settleable_leases(session, *, limit, settle):
    session.flush()
    count = 0
    for attempt_id, action_id in session.execute(_candidates(limit)).all():
        try:
            with session.begin_nested():
                changed = _settle_one(session, (attempt_id, action_id), settle=settle)
            count += changed
        except Exception:
            logger.exception("engagement_lease_recovery_failed action_id=%s attempt_id=%s", action_id, attempt_id)
    return count


def _settle_one(session, identity, *, settle):
    attempt_id, action_id = identity
    action = session.scalar(select(Action).where(Action.id == action_id)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if action is None:
        return 0
    attempt = session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.id == attempt_id)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if attempt is None:
        return 0
    session.scalar(select(AccountPoolConcurrencyLease)
        .where(AccountPoolConcurrencyLease.attempt_id == attempt_id)
        .with_for_update().execution_options(populate_existing=True))
    before = _resource_state(session, attempt_id)
    settle(attempt, action, remote_mutation_started=bool(attempt.gateway_call_started_at))
    session.flush()
    return int(_resource_state(session, attempt_id) != before)


def _resource_state(session, attempt_id):
    lease, budget, fence = AccountPoolConcurrencyLease, AccountBehaviorBudgetReservation, RemoteInvocationFence
    return session.execute(select(lease.state, budget.state, fence.state,
        fence.transport_termination_state, fence.business_outcome_state)
        .join(budget, budget.attempt_id == lease.attempt_id)
        .join(fence, fence.attempt_id == lease.attempt_id).where(lease.attempt_id == attempt_id)).one()
