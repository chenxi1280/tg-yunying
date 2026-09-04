from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import (
    Action,
    ContextTurn,
    ConversationEvent,
    ConversationTurnClaim,
    InteractionOpportunity,
)
from app.services._common import _now


REMOTE_CONTEXT_WINDOW_LIMIT = 10
MAX_NEWER_CONTEXT_MESSAGES = 5


@dataclass(frozen=True)
class RemoteContextDecision:
    allowed: bool
    reason: str = ""


def validate_remote_conversation_context(
    session: Session,
    action: Action,
    snapshots: list[object],
) -> RemoteContextDecision:
    payload = dict(action.payload or {})
    claim_id = str(payload.get("conversation_turn_claim_id") or "")
    if not claim_id:
        return RemoteContextDecision(True)
    claim = session.get(ConversationTurnClaim, claim_id)
    opportunity = _claim_opportunity(session, claim)
    event = _opportunity_event(session, opportunity)
    if claim is None or opportunity is None or event is None:
        return RemoteContextDecision(False, "conversation_binding_missing")
    parent_index = _parent_index(snapshots, event.remote_message_id)
    if parent_index is None:
        return _reject(session, claim, opportunity, "reply_parent_remote_missing_or_overtaken")
    snapshot = snapshots[parent_index]
    if _content_hash(getattr(snapshot, "content", "")) != event.content_hash:
        event.is_current = False
        return _reject(session, claim, opportunity, "reply_parent_remote_edited")
    payload_event_revision = payload.get("conversation_event_revision")
    if (
        payload_event_revision is not None
        and int(payload_event_revision) != int(event.event_revision or 1)
    ):
        return _reject(session, claim, opportunity, "conversation_event_revision_stale")
    if _newer_message_count(snapshots, parent_index) > MAX_NEWER_CONTEXT_MESSAGES:
        return _reject(session, claim, opportunity, "context_remote_topic_overtaken")
    turn = session.get(ContextTurn, claim.context_turn_id)
    if turn is None or int(payload.get("context_turn_revision") or 0) != int(turn.version or 1):
        return _reject(session, claim, opportunity, "context_turn_revision_stale")
    return RemoteContextDecision(True)


def _claim_opportunity(
    session: Session,
    claim: ConversationTurnClaim | None,
) -> InteractionOpportunity | None:
    return session.get(InteractionOpportunity, claim.interaction_opportunity_id) if claim else None


def _opportunity_event(
    session: Session,
    opportunity: InteractionOpportunity | None,
) -> ConversationEvent | None:
    return session.get(ConversationEvent, opportunity.anchor_event_id) if opportunity else None


def _parent_index(snapshots: list[object], remote_message_id: str) -> int | None:
    expected = str(remote_message_id)
    return next(
        (
            index
            for index, snapshot in enumerate(snapshots)
            if str(getattr(snapshot, "remote_message_id", "")) == expected
        ),
        None,
    )


def _newer_message_count(snapshots: list[object], parent_index: int) -> int:
    return sum(
        1
        for snapshot in snapshots[:parent_index]
        if str(getattr(snapshot, "remote_message_id", ""))
    )


def _reject(
    session: Session,
    claim: ConversationTurnClaim,
    opportunity: InteractionOpportunity,
    reason: str,
) -> RemoteContextDecision:
    claim.state = "stale"
    claim.settled_at = _now()
    claim.settlement_reason = reason
    opportunity.state = "stale"
    opportunity.updated_at = _now()
    session.flush()
    return RemoteContextDecision(False, reason)


def _content_hash(content: object) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


__all__ = [
    "REMOTE_CONTEXT_WINDOW_LIMIT",
    "RemoteContextDecision",
    "validate_remote_conversation_context",
]
