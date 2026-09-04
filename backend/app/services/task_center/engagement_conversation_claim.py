from __future__ import annotations

from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ContextTurn, ConversationEvent, ConversationTurnClaim, InteractionOpportunity
from app.services._common import _now
from app.timezone import as_beijing as _naive

REPLY_CLAIM_OPEN_STATES = frozenset({"claimed", "bound"})


def bind_conversation_turn_claim(
    session: Session,
    action: Action,
) -> None:
    payload = dict(action.payload or {})
    claim_id = str(payload.get("conversation_turn_claim_id") or "")
    if not claim_id:
        return
    # Production disables autoflush; a refresh must not erase this transaction's invalidation.
    session.flush()
    claim = session.scalar(select(ConversationTurnClaim).where(
        ConversationTurnClaim.id == claim_id,
    ).with_for_update().execution_options(populate_existing=True))
    if claim is None or not _claim_matches_action(claim, action):
        raise RuntimeError("conversation_turn_claim_mismatch")
    if claim.state not in REPLY_CLAIM_OPEN_STATES:
        raise RuntimeError("conversation_turn_claim_terminal")
    if claim.action_id and claim.action_id != action.id:
        raise RuntimeError("conversation_turn_claim_already_bound")
    if claim.action_id == action.id and claim.state == "bound":
        return
    claim.action_id = action.id
    claim.account_id = action.account_id
    claim.state = "bound"
    session.flush()


def validate_conversation_turn_claim_for_gateway(
    session: Session,
    action: Action,
    *,
    now_value: datetime | None = None,
) -> tuple[bool, str]:
    payload = dict(action.payload or {})
    claim_id = str(payload.get("conversation_turn_claim_id") or "")
    if not claim_id:
        return True, ""
    session.flush()
    claim = session.scalar(select(ConversationTurnClaim).where(
        ConversationTurnClaim.id == claim_id,
    ).with_for_update().execution_options(populate_existing=True))
    if claim is None or not _claim_matches_action(claim, action):
        return False, "conversation_turn_claim_mismatch"
    if claim.action_id != action.id or claim.state not in REPLY_CLAIM_OPEN_STATES:
        return False, "conversation_turn_claim_not_owned"
    opportunity = session.get(InteractionOpportunity, claim.interaction_opportunity_id)
    turn = session.get(ContextTurn, claim.context_turn_id)
    event = session.get(ConversationEvent, opportunity.anchor_event_id) if opportunity else None
    reason = _gateway_stale_reason(
        payload, opportunity=opportunity, turn=turn, event=event, now_value=now_value or _now(),
    )
    if reason:
        _settle_stale_claim(claim, opportunity, reason)
        return False, reason
    return True, ""


def settle_conversation_turn_claim(
    session: Session,
    action: Action,
    *,
    outcome: str,
) -> None:
    claim_id = str((action.payload or {}).get("conversation_turn_claim_id") or "")
    claim = session.get(ConversationTurnClaim, claim_id) if claim_id else None
    if claim is None or claim.action_id != action.id:
        return
    claim.state = outcome
    claim.settled_at = _now()
    claim.settlement_reason = outcome
    opportunity = session.get(InteractionOpportunity, claim.interaction_opportunity_id)
    if opportunity is not None:
        opportunity.state = "served" if outcome == "served" else outcome
        opportunity.updated_at = _now()
    from .engagement_interaction_continuity import (
        refresh_interaction_continuity_settlement,
    )

    refresh_interaction_continuity_settlement(session, action)


def _gateway_stale_reason(
    payload: dict,
    *,
    opportunity: InteractionOpportunity | None,
    turn: ContextTurn | None,
    event: ConversationEvent | None,
    now_value: datetime,
) -> str:
    if opportunity is None or turn is None or event is None:
        return "conversation_binding_missing"
    if opportunity.state != "admitted":
        return "interaction_opportunity_not_admitted"
    if _naive(opportunity.freshness_deadline_at) <= _naive(now_value):
        return "context_stale_deadline"
    if not event.is_current or event.deleted_at is not None:
        return "reply_parent_not_current"
    if str(event.remote_message_id) != str(payload.get("reply_to_message_id") or ""):
        return "reply_parent_revision_mismatch"
    payload_event_revision = payload.get("conversation_event_revision")
    if (
        payload_event_revision is not None
        and int(payload_event_revision) != int(event.event_revision or 1)
    ):
        return "conversation_event_revision_stale"
    if int(payload.get("context_turn_revision") or 0) != int(turn.version or 1):
        return "context_turn_revision_stale"
    return ""


def _settle_stale_claim(
    claim: ConversationTurnClaim,
    opportunity: InteractionOpportunity | None,
    reason: str,
) -> None:
    claim.state = "stale"
    claim.settled_at = _now()
    claim.settlement_reason = reason
    if opportunity is not None:
        opportunity.state = "stale"
        opportunity.updated_at = _now()


def _claim_matches_action(claim: ConversationTurnClaim, action: Action) -> bool:
    return (
        claim.tenant_id == action.tenant_id
        and claim.task_id == action.task_id
        and claim.task_lifecycle_epoch == int(action.task_lifecycle_epoch or 1)
        and (claim.account_id is None or claim.account_id == action.account_id)
        and action.account_id is not None
    )


def conversation_reply_target(
    session: Session,
    claim: ConversationTurnClaim,
    *,
    opportunity: InteractionOpportunity,
    turn: ContextTurn,
    event: ConversationEvent,
) -> dict:
    turn_content = _turn_content(session, turn)
    return {
        "message_id": int(event.remote_message_id),
        "author": event.author_name or "真人用户",
        "preview": event.content[:120],
        "content": turn_content or event.content,
        "source": "conversation_turn",
        "conversation_event_id": event.id,
        "conversation_event_revision": int(event.event_revision or 1),
        "context_turn_id": turn.id,
        "context_turn_revision": int(turn.version or 1),
        "interaction_opportunity_id": opportunity.id,
        "conversation_turn_claim_id": claim.id,
        "response_not_before_at": opportunity.natural_not_before_at.isoformat(),
        "freshness_deadline_at": opportunity.freshness_deadline_at.isoformat(),
    }


def _turn_content(session: Session, turn: ContextTurn) -> str:
    event_ids = [str(item) for item in turn.event_ids or [] if str(item)]
    if not event_ids:
        return ""
    rows = list(session.scalars(
        select(ConversationEvent).where(
            ConversationEvent.id.in_(event_ids),
            ConversationEvent.is_current.is_(True),
            ConversationEvent.deleted_at.is_(None),
        )
    ))
    by_id = {row.id: row for row in rows}
    parts = []
    for event_id in event_ids:
        event = by_id.get(event_id)
        if event is None:
            continue
        author = event.author_name or "真人用户"
        parts.append(f"{author}: {event.content}")
    return "\n".join(parts)
