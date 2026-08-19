from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    CommentFulfillmentObligation,
    ContentMixCycle,
    ContentMixCycleSlot,
    ContentMixObligation,
    FulfillmentObligationProjection,
    TaskGroupDailyMessageSlot,
)

from .content_mix_cycles import reconcile_content_mix_cycle


TERMINAL_SHORTFALL_STATE = "terminal_shortfall"


def project_generation_shortfall(
    session: Session,
    action: Action,
    *,
    reason_code: str,
) -> None:
    _close_obligation_projection(session, action)
    _close_comment_obligation(session, action)
    _close_content_mix_slot(session, action, reason_code=reason_code)


def _close_obligation_projection(session: Session, action: Action) -> None:
    projection = session.scalar(select(FulfillmentObligationProjection).where(
        FulfillmentObligationProjection.obligation_type == action.obligation_type,
        FulfillmentObligationProjection.obligation_id == action.obligation_id,
    ))
    if projection is None or projection.state == TERMINAL_SHORTFALL_STATE:
        return
    if projection.state != "open" or projection.active_action_id != action.id:
        raise RuntimeError("generation_shortfall_obligation_projection_conflict")
    projection.state = TERMINAL_SHORTFALL_STATE
    projection.version = int(projection.version or 1) + 1


def _close_comment_obligation(session: Session, action: Action) -> None:
    payload = dict(action.payload or {})
    obligation_id = str(payload.get("comment_fulfillment_obligation_id") or "")
    if not obligation_id:
        return
    obligation = session.get(CommentFulfillmentObligation, obligation_id)
    if obligation is None or obligation.current_action_id != action.id:
        raise RuntimeError("generation_shortfall_comment_obligation_conflict")
    if obligation.status == TERMINAL_SHORTFALL_STATE:
        return
    if obligation.status != "pending":
        raise RuntimeError("generation_shortfall_comment_obligation_not_pending")
    obligation.status = TERMINAL_SHORTFALL_STATE


def _close_content_mix_slot(
    session: Session,
    action: Action,
    *,
    reason_code: str,
) -> None:
    slot_id = str(action.content_mix_cycle_slot_id or "")
    if not slot_id:
        return
    slot = session.get(ContentMixCycleSlot, slot_id)
    if slot is None or slot.current_action_id != action.id:
        raise RuntimeError("generation_shortfall_content_mix_slot_conflict")
    if slot.slot_state == "terminal":
        return
    if slot.slot_state != "pending":
        raise RuntimeError("generation_shortfall_content_mix_slot_not_pending")
    quantity = session.get(TaskGroupDailyMessageSlot, slot.primary_quantity_slot_id)
    cycle = session.get(ContentMixCycle, slot.cycle_id)
    if quantity is None or cycle is None:
        raise RuntimeError("generation_shortfall_content_mix_owner_missing")
    slot.slot_state = "terminal"
    slot.terminal_reason = reason_code[:80]
    quantity.state = "terminal"
    _shortfall_content_obligations(session, action, slot)
    reconcile_content_mix_cycle(session, cycle)


def _shortfall_content_obligations(
    session: Session,
    action: Action,
    slot: ContentMixCycleSlot,
) -> None:
    obligations = session.scalars(select(ContentMixObligation).where(
        ContentMixObligation.assigned_cycle_slot_id == slot.id,
        ContentMixObligation.assigned_action_id == action.id,
        ContentMixObligation.status == "pending",
    ))
    for obligation in obligations:
        obligation.shortfall_count = obligation.required_count
        obligation.status = "shortfall"


__all__ = ["project_generation_shortfall"]
