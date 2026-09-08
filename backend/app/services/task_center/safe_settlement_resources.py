"""Release only pacing reservations owned by a safely settled action."""
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import AccountPacingReservation, Action
from .channel_action_lifecycle import validate_channel_action_resources_released


def _settle_action_pacing_reservation(
    session: Session,
    action_id: str,
    *,
    replan_same_obligation: bool,
) -> None:
    reservation = session.scalar(select(AccountPacingReservation).where(
        AccountPacingReservation.action_id == action_id,
    ))
    if reservation is None or reservation.state == "missed":
        return
    if reservation.state in {"reserved", "bound"}:
        reservation.state = "reserved" if replan_same_obligation else "missed"
        if replan_same_obligation:
            reservation.action_id = None
        reservation.version = int(reservation.version or 1) + 1


def _validate_safe_settlement_replay(
    session: Session,
    action: Action,
    replan_same_obligation: bool,
) -> None:
    if action.status != "skipped":
        raise RuntimeError("safe_settlement_replay_action_not_skipped")
    validate_channel_action_resources_released(
        session,
        action,
        replan_same_obligation=replan_same_obligation,
    )


