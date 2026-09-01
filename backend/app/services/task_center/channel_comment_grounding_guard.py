from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelCommentGroundingAssignment,
    ChannelMessage,
    TaskCommentCapacityReservation,
)
from sqlalchemy import select

from .channel_payloads import PostCommentPayload


def comment_grounding_send_blocker(
    session: Session,
    action: Action,
    payload: PostCommentPayload,
) -> str:
    if not payload.source_revision_id:
        return ""
    message = session.get(ChannelMessage, int(payload.channel_message_id or 0))
    if message is None or not message.comment_available:
        return "source_unavailable_before_send"
    if message.current_source_revision_id != payload.source_revision_id:
        return "source_revision_superseded_before_gateway"
    capacity_blocker = _capacity_send_blocker(session, action)
    if capacity_blocker:
        return capacity_blocker
    if not payload.grounding_assignment_id:
        return "" if payload.comment_fallback_intent_kind == "planned" else "grounding_assignment_missing"
    assignment = session.get(
        ChannelCommentGroundingAssignment,
        payload.grounding_assignment_id,
    )
    valid = bool(
        assignment
        and assignment.tenant_id == action.tenant_id
        and assignment.source_revision_id == payload.source_revision_id
        and assignment.target_ordinal == payload.target_ordinal
        and assignment.assignment_state == "active"
    )
    return "" if valid else "grounding_assignment_superseded"


def _capacity_send_blocker(session: Session, action: Action) -> str:
    reservation = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.action_id == action.id,
    ))
    if reservation is None:
        return "comment_capacity_reservation_missing"
    if reservation.reservation_state != "action_reserved":
        return "comment_capacity_reservation_not_action_reserved"
    return ""


__all__ = ["comment_grounding_send_blocker"]
