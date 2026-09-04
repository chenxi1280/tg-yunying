from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ContextTurn,
    ConversationEvent,
    ConversationTurnClaim,
    GroupContextMessage,
    InteractionOpportunity,
    StageWakeOutbox,
    Task,
    TgGroup,
)
from app.services._common import _now
from app.timezone import as_beijing

from .engagement_target_scope import (
    has_current_task_target_scope_claim,
)
from .engagement_burst import latest_author_turn, supersede_unissued_turn


MIN_IDLE_SECONDS = 2.5
EXPECTED_IDLE_SECONDS = 5.0
MAX_IDLE_SECONDS = 8.0
HARD_CAP_SECONDS = 12.0
SENTENCE_TERMINAL_PUNCTUATION = ("?", "!", "。", "？", "！", "...", "…", "~")


def _is_terminal_sentence(content: str) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    return any(text.endswith(p) for p in SENTENCE_TERMINAL_PUNCTUATION)


def _calculate_closed_at(
    last_event_at: datetime,
    content: str,
    *,
    first_event_at: datetime,
) -> datetime:
    idle = MIN_IDLE_SECONDS if _is_terminal_sentence(content) else EXPECTED_IDLE_SECONDS
    target_close = _naive(last_event_at) + timedelta(seconds=idle)
    hard_limit = _naive(first_event_at) + timedelta(seconds=HARD_CAP_SECONDS)
    return min(target_close, hard_limit)


TURN_ASSEMBLY_SECONDS = 5
RESPONSE_FRESHNESS_SECONDS = 90
from .engagement_conversation_claim import (
    REPLY_CLAIM_OPEN_STATES,
    conversation_reply_target as _reply_target,
    bind_conversation_turn_claim,
    validate_conversation_turn_claim_for_gateway,
    settle_conversation_turn_claim,
)


def project_group_context_message(
    session: Session,
    group: TgGroup,
    message: GroupContextMessage,
) -> ConversationEvent | None:
    if not _eligible_external_human_message(message):
        return None
    existing = session.scalar(select(ConversationEvent).where(
        ConversationEvent.source_context_message_id == message.id
    ))
    if existing is not None:
        return existing
    event_at = _naive(message.sent_at or message.created_at or _now())
    event = ConversationEvent(
        tenant_id=group.tenant_id,
        surface="group_ai_chat",
        canonical_peer_id=_canonical_peer(group.tg_peer_id),
        target_group_id=group.id,
        remote_message_id=str(message.remote_message_id),
        event_revision=1,
        author_class="external_human",
        author_peer_id=str(message.sender_peer_id or ""),
        author_name=str(message.sender_name or ""),
        content=str(message.content or ""),
        content_hash=_content_hash(message.content),
        modality=str(message.message_type or "text"),
        source_context_message_id=message.id,
        sent_at=event_at,
        observed_at=_now(),
    )
    session.add(event)
    session.flush()
    _append_event_to_turn(session, group, event)
    return event


def apply_group_context_message_change(
    session: Session,
    task: Task,
    group: TgGroup,
    *,
    payload: dict,
    deleted: bool,
) -> bool:
    del task
    if not deleted:
        return False
    remote_id = str(payload.get("source_message_id") or "")
    message = session.scalar(select(GroupContextMessage).where(
        GroupContextMessage.group_id == group.id,
        GroupContextMessage.remote_message_id == remote_id,
    ).with_for_update())
    if message is None:
        return False
    event = session.scalar(select(ConversationEvent).where(
        ConversationEvent.source_context_message_id == message.id,
    ).with_for_update())
    if event is None:
        return False
    if event.deleted_at is not None:
        return False
    turns = _event_turns(session, event)
    _invalidate_open_turns(session, turns, reason="reply_parent_deleted")
    event.event_revision = int(event.event_revision or 1) + 1
    event.observed_at = _now()
    event.is_current = False
    event.deleted_at = _now()
    event.content = ""
    event.content_hash = _content_hash("")
    event.modality = "deleted"
    message.content = ""
    message.message_type = "deleted"
    return True


def _event_turns(session: Session, event: ConversationEvent) -> list[ContextTurn]:
    candidates = list(session.scalars(select(ContextTurn).where(
        ContextTurn.tenant_id == event.tenant_id,
        ContextTurn.surface == event.surface,
        ContextTurn.canonical_peer_id == event.canonical_peer_id,
    ).with_for_update()))
    return [turn for turn in candidates if event.id in list(turn.event_ids or [])]


def _invalidate_open_turns(session: Session, turns: list[ContextTurn], *, reason: str) -> None:
    for turn in turns:
        turn.version = int(turn.version or 1) + 1
        turn.updated_at = _now()
        opportunities = list(session.scalars(select(InteractionOpportunity).where(
            InteractionOpportunity.context_turn_id == turn.id,
            InteractionOpportunity.state == "admitted",
        ).with_for_update()))
        for opportunity in opportunities:
            opportunity.state = "stale"
            opportunity.updated_at = _now()
            _invalidate_opportunity_claim(session, opportunity, reason)


def _invalidate_opportunity_claim(session, opportunity, reason: str) -> None:
    claim = session.scalar(select(ConversationTurnClaim).where(
        ConversationTurnClaim.interaction_opportunity_id == opportunity.id,
        ConversationTurnClaim.state.in_(REPLY_CLAIM_OPEN_STATES),
    ).with_for_update())
    if claim is None:
        return
    claim.state = "stale"
    claim.settled_at = _now()
    claim.settlement_reason = reason


def interaction_reply_targets(
    session: Session,
    task: Task,
    group: TgGroup,
    *,
    context_rows: list[GroupContextMessage],
    now_value: datetime | None = None,
    limit: int = 20,
) -> list[dict]:
    if not _unified_group_task(task) or not has_current_task_target_scope_claim(session, task):
        return []
    for row in sorted(context_rows, key=_context_order):
        project_group_context_message(session, group, row)
    current = _naive(now_value or _now())
    materialize_due_turns(session, task, group, now_value=current)
    _expire_open_opportunities(session, task, current)
    rows = session.execute(
        select(ConversationTurnClaim, InteractionOpportunity, ContextTurn, ConversationEvent)
        .join(InteractionOpportunity, InteractionOpportunity.id == ConversationTurnClaim.interaction_opportunity_id)
        .join(ContextTurn, ContextTurn.id == ConversationTurnClaim.context_turn_id)
        .join(ConversationEvent, ConversationEvent.id == InteractionOpportunity.anchor_event_id)
        .where(
            ConversationTurnClaim.task_id == task.id,
            ConversationTurnClaim.task_lifecycle_epoch == int(task.task_lifecycle_epoch or 1),
            ConversationTurnClaim.state == "claimed",
            InteractionOpportunity.state == "admitted",
            InteractionOpportunity.freshness_deadline_at > current,
            ConversationEvent.is_current.is_(True),
            ConversationEvent.deleted_at.is_(None),
        )
        .order_by(ContextTurn.closed_at.asc(), ContextTurn.id.asc())
        .limit(max(1, int(limit)))
    ).all()
    return [
        _reply_target(session, claim, opportunity=opportunity, turn=turn, event=event)
        for claim, opportunity, turn, event in rows
    ]


def _append_event_to_turn(
    session: Session,
    group: TgGroup,
    event: ConversationEvent,
) -> ContextTurn:
    turn = latest_author_turn(session, event, state="assembling")
    if turn is not None and _same_burst(turn, event):
        turn.event_ids = [*list(turn.event_ids or []), event.id]
        turn.anchor_event_id = event.id
        turn.last_event_at = event.sent_at
        turn.closed_at = _calculate_closed_at(
            event.sent_at, event.content, first_event_at=turn.first_event_at
        )
        turn.event_count = len(turn.event_ids)
        turn.version = int(turn.version or 1) + 1
        turn.updated_at = _now()
        _enqueue_turn_close(session, turn)
        return turn

    recent_closed = latest_author_turn(session, event, state="closed")
    predecessor = None
    if recent_closed is not None and _same_burst(recent_closed, event):
        if supersede_unissued_turn(session, recent_closed, invalidate=_invalidate_open_turns):
            predecessor = recent_closed
    return _new_burst_turn(session, group, event, predecessor=predecessor)


def _new_burst_turn(session, group, event, *, predecessor):
    event_ids = [*list(predecessor.event_ids or []), event.id] if predecessor else [event.id]
    first_at = predecessor.first_event_at if predecessor else event.sent_at
    turn = ContextTurn(
        tenant_id=group.tenant_id,
        surface="group_ai_chat",
        canonical_peer_id=event.canonical_peer_id,
        target_group_id=group.id,
        turn_family_key=f"{event.canonical_peer_id}:{event.remote_message_id}",
        author_peer_id=str(event.author_peer_id or ""),
        anchor_event_id=event.id,
        event_ids=event_ids,
        event_count=len(event_ids),
        first_event_at=first_at,
        last_event_at=event.sent_at,
        closed_at=_calculate_closed_at(
            event.sent_at, event.content, first_event_at=first_at
        ),
    )
    session.add(turn)
    session.flush()
    _enqueue_turn_close(session, turn)
    return turn


def materialize_due_turns(
    session: Session,
    task: Task,
    group: TgGroup,
    *,
    now_value: datetime,
) -> None:
    current = _naive(now_value)
    turns = list(session.scalars(
        select(ContextTurn).where(
            ContextTurn.tenant_id == task.tenant_id,
            ContextTurn.surface == "group_ai_chat",
            ContextTurn.canonical_peer_id == _canonical_peer(group.tg_peer_id),
            ContextTurn.state == "assembling",
            ContextTurn.closed_at <= current,
        ).order_by(ContextTurn.closed_at, ContextTurn.id).with_for_update(skip_locked=True)
    ))
    for turn in turns:
        materialize_turn(session, task, turn, current=current)


def materialize_turn(session, task, turn, *, current):
    turn.state = "closed"
    turn.updated_at = _now()
    opportunity = _ensure_opportunity(session, task, turn)
    if _naive(opportunity.freshness_deadline_at) <= current:
        opportunity.state = "missed"
    else:
        _ensure_turn_claim(session, task, turn, opportunity=opportunity)
    _deliver_turn_wakes(session, turn)


def _ensure_opportunity(
    session: Session,
    task: Task,
    turn: ContextTurn,
) -> InteractionOpportunity:
    epoch = int(task.task_lifecycle_epoch or 1)
    existing = session.scalar(select(InteractionOpportunity).where(
        InteractionOpportunity.task_id == task.id,
        InteractionOpportunity.task_lifecycle_epoch == epoch,
        InteractionOpportunity.context_turn_id == turn.id,
    ))
    if existing is not None:
        return existing
    opportunity = InteractionOpportunity(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_lifecycle_epoch=epoch,
        context_turn_id=turn.id,
        anchor_event_id=turn.anchor_event_id,
        natural_not_before_at=turn.closed_at,
        freshness_deadline_at=turn.closed_at + timedelta(seconds=RESPONSE_FRESHNESS_SECONDS),
    )
    session.add(opportunity)
    session.flush()
    return opportunity


def _ensure_turn_claim(
    session: Session,
    task: Task,
    turn: ContextTurn,
    *,
    opportunity: InteractionOpportunity,
) -> ConversationTurnClaim:
    existing = session.scalar(select(ConversationTurnClaim).where(
        ConversationTurnClaim.context_turn_id == turn.id
    ))
    if existing is not None:
        return existing
    claim = ConversationTurnClaim(
        tenant_id=task.tenant_id,
        context_turn_id=turn.id,
        interaction_opportunity_id=opportunity.id,
        task_id=task.id,
        task_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
    )
    session.add(claim)
    session.flush()
    return claim


def _expire_open_opportunities(
    session: Session,
    task: Task,
    now_value: datetime,
) -> None:
    opportunities = list(session.scalars(select(InteractionOpportunity).where(
        InteractionOpportunity.task_id == task.id,
        InteractionOpportunity.task_lifecycle_epoch == int(task.task_lifecycle_epoch or 1),
        InteractionOpportunity.state == "admitted",
        InteractionOpportunity.freshness_deadline_at <= now_value,
    )))
    for opportunity in opportunities:
        opportunity.state = "missed"
        opportunity.updated_at = _now()
        claim = session.scalar(select(ConversationTurnClaim).where(
            ConversationTurnClaim.interaction_opportunity_id == opportunity.id,
            ConversationTurnClaim.state.in_(REPLY_CLAIM_OPEN_STATES),
        ))
        if claim is not None:
            claim.state = "missed"
            claim.settled_at = _now()
            claim.settlement_reason = "freshness_deadline_exceeded"


def _enqueue_turn_close(session: Session, turn: ContextTurn) -> None:
    existing = session.scalar(select(StageWakeOutbox.id).where(
        StageWakeOutbox.aggregate_type == "context_turn",
        StageWakeOutbox.aggregate_id == turn.id,
        StageWakeOutbox.aggregate_revision == turn.version,
        StageWakeOutbox.stage == "close_turn",
    ))
    if existing is not None:
        return
    session.add(StageWakeOutbox(
        tenant_id=turn.tenant_id,
        aggregate_type="context_turn",
        aggregate_id=turn.id,
        aggregate_revision=turn.version,
        stage="close_turn",
        available_at=turn.closed_at,
    ))


def _deliver_turn_wakes(session: Session, turn: ContextTurn) -> None:
    wakes = list(session.scalars(select(StageWakeOutbox).where(
        StageWakeOutbox.aggregate_type == "context_turn",
        StageWakeOutbox.aggregate_id == turn.id,
        StageWakeOutbox.stage == "close_turn",
        StageWakeOutbox.state == "pending",
    ).with_for_update(nowait=True)))
    for wake in wakes:
        wake.state = (
            "delivered"
            if int(wake.aggregate_revision) == int(turn.version or 1)
            else "superseded"
        )
        wake.delivered_at = _now()
        wake.attempt_count = int(wake.attempt_count or 0) + 1


def _same_burst(turn: ContextTurn, event: ConversationEvent) -> bool:
    if turn.author_peer_id and event.author_peer_id and str(turn.author_peer_id) != str(event.author_peer_id):
        return False
    delta = _naive(event.sent_at) - _naive(turn.last_event_at)
    total_duration = _naive(event.sent_at) - _naive(turn.first_event_at)
    return (
        timedelta(0) <= delta <= timedelta(seconds=MAX_IDLE_SECONDS)
        and total_duration <= timedelta(seconds=HARD_CAP_SECONDS)
    )


def _eligible_external_human_message(message: GroupContextMessage) -> bool:
    role = str(message.sender_role or "").lower()
    return (
        bool(message.id)
        and not message.is_bot
        and role not in {"system", "service"}
        and str(message.remote_message_id or "").isdigit()
        and bool(str(message.content or "").strip())
    )


def _unified_group_task(task: Task) -> bool:
    return (
        task.type == "group_ai_chat"
        and (task.type_config or {}).get("engagement_contract_version")
        == "unified_engagement_v1"
    )


def _context_order(message: GroupContextMessage) -> tuple[datetime, int]:
    return _naive(message.sent_at or message.created_at or _now()), int(message.id or 0)


def _content_hash(content: str) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def _canonical_peer(value: str) -> str:
    peer = str(value or "").strip()
    if not peer:
        raise ValueError("conversation_peer_missing")
    return peer


def _naive(value: datetime) -> datetime:
    return as_beijing(value)


__all__ = [
    "apply_group_context_message_change",
    "bind_conversation_turn_claim",
    "interaction_reply_targets",
    "materialize_due_turns",
    "materialize_turn",
    "project_group_context_message",
    "settle_conversation_turn_claim",
    "validate_conversation_turn_claim_for_gateway",
]
