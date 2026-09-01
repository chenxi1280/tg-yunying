from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountPacingReservation, Action


def release_action_pacing_reservation_before_gateway(
    session: Session,
    action: Action,
) -> None:
    reservation = session.scalar(select(AccountPacingReservation).where(
        AccountPacingReservation.action_id == action.id,
    ))
    if reservation is None:
        return
    if reservation.state not in {"reserved", "bound"}:
        raise RuntimeError("account_pacing_reservation_not_releasable")
    reservation.action_id = None
    reservation.state = "reserved"
    reservation.version = int(reservation.version or 1) + 1


__all__ = ["release_action_pacing_reservation_before_gateway"]
