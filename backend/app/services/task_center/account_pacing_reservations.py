from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountPacingReservation,
    Action,
    ExecutionAttempt,
    FulfillmentRemoteFact,
    Task,
)


OPEN_RESERVATION_STATES = ("reserved", "bound")
REUSABLE_TERMINAL_ACTION_STATUSES = frozenset({"failed", "skipped"})


def reservation_for_slot(
    session: Session,
    tenant_id: int,
    account_id: int,
    slot_key: str,
) -> AccountPacingReservation | None:
    return session.scalar(select(AccountPacingReservation).where(
        AccountPacingReservation.tenant_id == tenant_id,
        AccountPacingReservation.account_id == account_id,
        AccountPacingReservation.pacing_slot_key == slot_key,
        AccountPacingReservation.state.in_(OPEN_RESERVATION_STATES),
    ))


def reservation_for_any_slot(
    session: Session,
    tenant_id: int,
    account_id: int,
    slot_key: str,
) -> AccountPacingReservation | None:
    return session.scalar(select(AccountPacingReservation).where(
        AccountPacingReservation.tenant_id == tenant_id,
        AccountPacingReservation.account_id == account_id,
        AccountPacingReservation.pacing_slot_key == slot_key,
    ))


def bind_account_pacing_reservation(
    reservation: AccountPacingReservation,
    action: Action,
) -> None:
    reservation.action_id = action.id
    reservation.state = "bound"
    reservation.version += 1
    action.scheduled_at = reservation.effective_claim_at
    action.release_not_before_at = reservation.release_not_before_at
    action.effective_claim_at = reservation.effective_claim_at


def bind_account_pacing_reservation_for_slot(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    slot_key: str,
    action: Action,
) -> None:
    reservation = reservation_for_slot(
        session,
        tenant_id,
        account_id,
        slot_key,
    )
    if reservation is None:
        raise ValueError("account_pacing_reservation_missing")
    bind_account_pacing_reservation(reservation, action)


def release_unbound_account_pacing_reservation(
    reservation: AccountPacingReservation,
) -> None:
    if reservation.state != "reserved" or reservation.action_id is not None:
        raise ValueError("account_pacing_reservation_not_releasable")
    reservation.state = "released"
    reservation.version = int(reservation.version or 1) + 1


def release_safe_task_account_pacing_reservations(
    session: Session,
    task: Task,
) -> int:
    session.flush()
    statement = select(AccountPacingReservation).where(
        AccountPacingReservation.task_id == task.id,
        AccountPacingReservation.state.in_(OPEN_RESERVATION_STATES),
        (
            AccountPacingReservation.action_id.is_not(None)
            | (AccountPacingReservation.state == "bound")
        ),
    )
    if session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update(of=AccountPacingReservation)
    reservations = list(session.scalars(statement))
    action_ids = [row.action_id for row in reservations if row.action_id]
    actions = _actions_by_id(session, action_ids)
    remote_bound = _remote_bound_action_ids(session, action_ids)
    released = 0
    for reservation in reservations:
        action = actions.get(str(reservation.action_id or ""))
        if not _reservation_reusable(action, remote_bound):
            continue
        reservation.action_id = None
        reservation.state = "reserved"
        reservation.version = int(reservation.version or 1) + 1
        released += 1
    return released


def _actions_by_id(session: Session, action_ids: list[str]) -> dict[str, Action]:
    if not action_ids:
        return {}
    return {
        row.id: row
        for row in session.scalars(select(Action).where(Action.id.in_(action_ids)))
    }


def _remote_bound_action_ids(session: Session, action_ids: list[str]) -> set[str]:
    if not action_ids:
        return set()
    attempts = set(session.scalars(select(ExecutionAttempt.action_id).where(
        ExecutionAttempt.action_id.in_(action_ids),
        (ExecutionAttempt.gateway_call_started_at.is_not(None))
        | (ExecutionAttempt.remote_message_id != ""),
    )))
    facts = set(session.scalars(select(FulfillmentRemoteFact.action_id).where(
        FulfillmentRemoteFact.action_id.in_(action_ids),
    )))
    return attempts | facts


def _reservation_reusable(
    action: Action | None,
    remote_bound: set[str],
) -> bool:
    if action is None:
        return True
    if action.status not in REUSABLE_TERMINAL_ACTION_STATUSES:
        return False
    result = dict(action.result or {})
    return (
        action.id not in remote_bound
        and not result.get("gateway_call_started_at")
        and not result.get("remote_message_id")
    )


__all__ = [
    "bind_account_pacing_reservation",
    "bind_account_pacing_reservation_for_slot",
    "release_unbound_account_pacing_reservation",
    "release_safe_task_account_pacing_reservations",
    "reservation_for_any_slot",
    "reservation_for_slot",
]
