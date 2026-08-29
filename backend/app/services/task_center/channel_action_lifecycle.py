from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    AccountPacingReservation,
    Action,
    ChannelViewDailyIdentityOwner,
    ExecutionAttempt,
    ReactionFulfillmentObligation,
    SourcePacingAdmission,
    SourcePacingState,
    ViewFulfillmentObligation,
)
from .channel_view_daily_identity import (
    mark_daily_identity_unknown,
    release_daily_identity,
)


def release_channel_action_before_gateway(
    session: Session,
    action: Action,
    *,
    remote_mutation_state: str | None = None,
) -> None:
    if action.action_type not in {"like_message", "view_message"}:
        return
    if action.action_type == "view_message" and not release_daily_identity(
        session,
        action,
        remote_mutation_state=remote_mutation_state,
    ):
        raise RuntimeError("channel_view_daily_identity_safe_release_failed")
    obligation = _locked_payload_obligation(session, action)
    if obligation is None:
        if action.action_type == "like_message":
            return
        raise RuntimeError("channel_action_safe_release_obligation_missing")
    if obligation.status == "confirmed":
        raise RuntimeError("confirmed_channel_obligation_cannot_reopen")
    if obligation.current_action_id is None:
        if obligation.status != "open":
            raise RuntimeError("released_channel_obligation_state_invalid")
        return
    if obligation.current_action_id != action.id:
        raise RuntimeError("channel_action_safe_release_obligation_owner_mismatch")
    obligation.current_action_id = None
    obligation.status = "open"


def release_channel_action_resources_before_gateway(
    session: Session,
    action: Action,
    *,
    remote_mutation_state: str | None = None,
    replan_same_obligation: bool = False,
) -> set[str]:
    if action.action_type not in {"view_message", "like_message"}:
        return set()
    result = dict(action.result or {})
    terminal = (
        action.status == "skipped"
        or result.get("account_task_disposition") == "abandoned"
    )
    if not terminal:
        return set()
    bound_obligation = _bound_obligation(session, action)
    daily_owner_exists = (
        action.action_type == "view_message" and _daily_owner_exists(session, action)
    )
    if bound_obligation is None and not action.pacing_slot_key and not daily_owner_exists:
        return set()
    release_channel_action_before_gateway(
        session,
        action,
        remote_mutation_state=remote_mutation_state,
    )
    if not action.pacing_slot_key:
        return set()
    _settle_pacing_reservation(
        session,
        action.id,
        replan_same_obligation=replan_same_obligation,
    )
    return _cancel_pre_gateway_source_admissions(session, action.id)


def hold_channel_action_after_gateway(session: Session, action: Action) -> None:
    if action.action_type == "view_message":
        mark_daily_identity_unknown(session, action)
    obligation = _bound_obligation(session, action)
    if obligation is None:
        if action.action_type == "like_message":
            return
        raise RuntimeError("channel_action_gateway_hold_obligation_missing")
    if obligation.status == "confirmed":
        raise RuntimeError("confirmed_channel_obligation_cannot_mark_unknown")
    obligation.status = "unknown"


def validate_channel_action_resources_released(
    session: Session,
    action: Action,
) -> None:
    obligation = _payload_obligation(session, action)
    if obligation is not None and (
        obligation.status != "open" or obligation.current_action_id is not None
    ):
        raise RuntimeError("safe_settlement_replay_obligation_not_released")
    if action.action_type == "view_message" and _daily_owner_exists(session, action):
        raise RuntimeError("safe_settlement_replay_daily_owner_not_released")
    reservation = session.scalar(select(AccountPacingReservation).where(
        AccountPacingReservation.action_id == action.id,
    ))
    if reservation is not None and reservation.state != "missed":
        raise RuntimeError("safe_settlement_replay_pacing_reservation_not_released")
    admission_id = session.scalar(select(SourcePacingAdmission.id).where(
        SourcePacingAdmission.action_id == action.id,
        SourcePacingAdmission.state.in_(("reserved", "finished")),
    ).limit(1))
    if admission_id is not None:
        raise RuntimeError("safe_settlement_replay_source_admission_not_released")


def _payload_obligation(
    session: Session,
    action: Action,
) -> ReactionFulfillmentObligation | ViewFulfillmentObligation | None:
    contract = {
        "like_message": (
            ReactionFulfillmentObligation,
            "reaction_fulfillment_obligation_id",
        ),
        "view_message": (
            ViewFulfillmentObligation,
            "view_fulfillment_obligation_id",
        ),
    }.get(action.action_type)
    if contract is None:
        return None
    model, payload_key = contract
    payload = action.payload if isinstance(action.payload, dict) else {}
    return session.get(model, str(payload.get(payload_key) or ""))


def _daily_owner_exists(session: Session, action: Action) -> bool:
    payload = action.payload if isinstance(action.payload, dict) else {}
    obligation_id = str(payload.get("view_fulfillment_obligation_id") or "")
    return session.scalar(select(ChannelViewDailyIdentityOwner.id).where(
        (ChannelViewDailyIdentityOwner.action_id == action.id)
        | (ChannelViewDailyIdentityOwner.obligation_id == obligation_id)
    ).limit(1)) is not None


def _bound_obligation(
    session: Session,
    action: Action,
) -> ReactionFulfillmentObligation | ViewFulfillmentObligation | None:
    contract = {
        "like_message": (
            ReactionFulfillmentObligation,
            "reaction_fulfillment_obligation_id",
        ),
        "view_message": (
            ViewFulfillmentObligation,
            "view_fulfillment_obligation_id",
        ),
    }.get(action.action_type)
    if contract is None:
        return None
    model, payload_key = contract
    payload = action.payload if isinstance(action.payload, dict) else {}
    obligation = session.scalar(
        select(model)
        .where(model.id == str(payload.get(payload_key) or ""))
        .with_for_update()
    )
    if obligation is None or obligation.current_action_id != action.id:
        return None
    return obligation


def _locked_payload_obligation(
    session: Session,
    action: Action,
) -> ReactionFulfillmentObligation | ViewFulfillmentObligation | None:
    contract = {
        "like_message": (
            ReactionFulfillmentObligation,
            "reaction_fulfillment_obligation_id",
        ),
        "view_message": (
            ViewFulfillmentObligation,
            "view_fulfillment_obligation_id",
        ),
    }.get(action.action_type)
    if contract is None:
        return None
    model, payload_key = contract
    payload = action.payload if isinstance(action.payload, dict) else {}
    return session.scalar(
        select(model)
        .where(model.id == str(payload.get(payload_key) or ""))
        .with_for_update()
    )


def _cancel_pre_gateway_source_admissions(
    session: Session,
    action_id: str,
) -> set[str]:
    state_ids = set(session.scalars(
        select(SourcePacingAdmission.source_pacing_state_id)
        .where(
            SourcePacingAdmission.action_id == action_id,
            SourcePacingAdmission.state.in_(("reserved", "finished")),
        )
        .distinct()
    ))
    if not state_ids:
        return set()
    list(session.scalars(
        select(SourcePacingState)
        .where(SourcePacingState.id.in_(state_ids))
        .order_by(SourcePacingState.id)
        .with_for_update()
    ))
    rows = session.scalars(
        select(SourcePacingAdmission)
        .outerjoin(ExecutionAttempt, ExecutionAttempt.id == SourcePacingAdmission.attempt_id)
        .where(
            SourcePacingAdmission.action_id == action_id,
            SourcePacingAdmission.state.in_(("reserved", "finished")),
            or_(
                SourcePacingAdmission.attempt_id.is_(None),
                ExecutionAttempt.gateway_call_started_at.is_(None),
            ),
        )
        .with_for_update(of=SourcePacingAdmission)
    )
    cancelled: set[str] = set()
    for admission in rows:
        admission.state = "cancelled_pre_gateway"
        admission.version = int(admission.version or 1) + 1
        cancelled.add(admission.source_pacing_state_id)
    return cancelled


def _settle_pacing_reservation(
    session: Session,
    action_id: str,
    *,
    replan_same_obligation: bool,
) -> None:
    reservation = session.scalar(select(AccountPacingReservation).where(
        AccountPacingReservation.action_id == action_id,
    ))
    if reservation is None:
        raise RuntimeError("pacing_claim_reservation_missing")
    if reservation.state == "missed":
        return
    if reservation.state not in {"reserved", "bound"}:
        raise RuntimeError(
            f"pacing_claim_reservation_state_invalid:{reservation.state}"
        )
    reservation.state = "reserved" if replan_same_obligation else "missed"
    if replan_same_obligation:
        reservation.action_id = None
    reservation.version = int(reservation.version or 1) + 1


__all__ = [
    "hold_channel_action_after_gateway",
    "release_channel_action_before_gateway",
    "release_channel_action_resources_before_gateway",
    "validate_channel_action_resources_released",
]
