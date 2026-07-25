"""Persistent conversation speaker rotation for group_ai_chat and channel_comment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ConversationSpeakerState, ConversationSpeakerTurn
from app.models.enums import now as model_now


PLATFORM_HOLDS = {"success", "unknown_after_send", "pending_visibility"}
HUMAN_KIND = "human"
PLATFORM_KIND = "platform"
CONTROL_KIND = "group_bot_control"
SYSTEM_KIND = "system"


@dataclass(frozen=True)
class SpeakerDecision:
    allowed: bool
    account_id: int | None = None
    code: str = ""
    reason: str = ""


def conversation_key_for_group(*, group_id: int) -> str:
    return f"group:{int(group_id)}"


def conversation_key_for_discussion(*, discussion_group_id: int) -> str:
    return f"discussion:{int(discussion_group_id)}"


def lock_or_create_state(
    session: Session,
    *,
    tenant_id: int,
    surface: str,
    conversation_key: str,
) -> ConversationSpeakerState:
    state = session.scalar(
        select(ConversationSpeakerState)
        .where(
            ConversationSpeakerState.tenant_id == tenant_id,
            ConversationSpeakerState.surface == surface,
            ConversationSpeakerState.conversation_key == conversation_key,
        )
        .with_for_update()
    )
    if state is not None:
        return state
    state = ConversationSpeakerState(
        tenant_id=tenant_id,
        surface=surface,
        conversation_key=conversation_key,
    )
    session.add(state)
    session.flush()
    # Re-lock after insert race.
    locked = session.scalar(
        select(ConversationSpeakerState)
        .where(ConversationSpeakerState.id == state.id)
        .with_for_update()
    )
    return locked or state


def human_has_broken_adjacency(session: Session, state: ConversationSpeakerState) -> bool:
    """True when a human message was observed after the last platform hold."""
    if not state.last_platform_action_id and not state.last_platform_account_id:
        return True
    last_platform = session.scalar(
        select(ConversationSpeakerTurn)
        .where(
            ConversationSpeakerTurn.tenant_id == state.tenant_id,
            ConversationSpeakerTurn.surface == state.surface,
            ConversationSpeakerTurn.conversation_key == state.conversation_key,
            ConversationSpeakerTurn.sender_kind == PLATFORM_KIND,
        )
        .order_by(ConversationSpeakerTurn.observed_at.desc(), ConversationSpeakerTurn.id.desc())
        .limit(1)
    )
    if last_platform is None:
        return True
    human_after = session.scalar(
        select(ConversationSpeakerTurn.id)
        .where(
            ConversationSpeakerTurn.tenant_id == state.tenant_id,
            ConversationSpeakerTurn.surface == state.surface,
            ConversationSpeakerTurn.conversation_key == state.conversation_key,
            ConversationSpeakerTurn.sender_kind == HUMAN_KIND,
            ConversationSpeakerTurn.observed_at >= last_platform.observed_at,
            ConversationSpeakerTurn.id > last_platform.id,
        )
        .limit(1)
    )
    if human_after is not None:
        return True
    if state.last_human_cursor and last_platform.remote_cursor:
        try:
            return str(state.last_human_cursor) > str(last_platform.remote_cursor)
        except Exception:
            return False
    return False


def reserve_speaker_turn(
    session: Session,
    *,
    action: Action,
    surface: str,
    conversation_key: str,
    candidate_account_ids: list[int],
    coverage_bound: bool = False,
) -> SpeakerDecision:
    candidates = [int(item) for item in candidate_account_ids if int(item or 0) > 0]
    if not candidates:
        return SpeakerDecision(False, code="speaker_rotation_no_candidates", reason="no_candidates")

    state = lock_or_create_state(
        session,
        tenant_id=action.tenant_id,
        surface=surface,
        conversation_key=conversation_key,
    )
    blocked_id = state.reserved_account_id or state.last_platform_account_id
    if blocked_id and not human_has_broken_adjacency(session, state):
        alternate = next((item for item in candidates if item != int(blocked_id)), None)
        if alternate is None:
            # Group AI hard-hourly: wait so capacity math treats single-account as unsustainable.
            # Channel comment often runs with one discussion account; allow with explicit warning.
            if surface == "channel_comment" and len(candidates) == 1:
                selected = candidates[0]
                reason = "single_account_capacity_warning"
            else:
                return SpeakerDecision(
                    False,
                    account_id=int(blocked_id),
                    code="speaker_rotation_wait",
                    reason="no_alternate_account",
                )
        else:
            selected = alternate
            reason = "rotated_from_last_speaker"
    else:
        preferred = int(action.account_id or 0)
        selected = preferred if preferred in candidates else candidates[0]
        reason = "human_break_or_first_speaker"

    if coverage_bound and int(action.account_id or 0) and selected != int(action.account_id):
        return SpeakerDecision(
            False,
            account_id=int(action.account_id),
            code="speaker_rotation_wait",
            reason="coverage_ledger_bound",
        )

    state.reserved_account_id = selected
    state.reserved_action_id = action.id
    state.reserved_at = model_now()
    state.version = int(state.version or 1) + 1
    session.flush()
    return SpeakerDecision(True, account_id=selected, reason=reason)


def release_speaker_reservation(
    session: Session,
    *,
    action: Action,
    surface: str,
    conversation_key: str,
) -> None:
    state = session.scalar(
        select(ConversationSpeakerState)
        .where(
            ConversationSpeakerState.tenant_id == action.tenant_id,
            ConversationSpeakerState.surface == surface,
            ConversationSpeakerState.conversation_key == conversation_key,
        )
        .with_for_update()
    )
    if state is None:
        return
    if state.reserved_action_id and state.reserved_action_id != action.id:
        return
    state.reserved_account_id = None
    state.reserved_action_id = None
    state.reserved_at = None
    state.version = int(state.version or 1) + 1
    session.flush()


def finalize_speaker_turn(
    session: Session,
    *,
    action: Action,
    surface: str,
    conversation_key: str,
    outcome: str,
    remote_message_id: str = "",
    remote_cursor: str = "",
    content_source: str = "",
    content_preview: str = "",
    account_id: int | None = None,
) -> None:
    state = lock_or_create_state(
        session,
        tenant_id=action.tenant_id,
        surface=surface,
        conversation_key=conversation_key,
    )
    if state.reserved_action_id and state.reserved_action_id not in {None, action.id}:
        # Another action owns the reservation; still record outcome turns for audit when remote id exists.
        pass
    elif state.reserved_action_id == action.id:
        state.reserved_account_id = None
        state.reserved_action_id = None
        state.reserved_at = None

    account = int(account_id or action.account_id or 0) or None
    preview = str(content_preview or "").strip()[:200]
    source = str(content_source or "").strip()
    if not source and preview == "签到":
        source = "check_in_fallback"
    if outcome in PLATFORM_HOLDS:
        state.last_platform_account_id = account
        state.last_platform_action_id = action.id
        state.last_platform_outcome = outcome
        state.last_platform_content_source = source
        state.version = int(state.version or 1) + 1

    remote_id = str(remote_message_id or "").strip() or f"local:{action.id}:{outcome}"
    existing = session.scalar(
        select(ConversationSpeakerTurn).where(
            ConversationSpeakerTurn.tenant_id == action.tenant_id,
            ConversationSpeakerTurn.surface == surface,
            ConversationSpeakerTurn.conversation_key == conversation_key,
            ConversationSpeakerTurn.remote_message_id == remote_id,
        )
    )
    if existing is None:
        session.add(
            ConversationSpeakerTurn(
                tenant_id=action.tenant_id,
                surface=surface,
                conversation_key=conversation_key,
                remote_message_id=remote_id,
                remote_cursor=str(remote_cursor or remote_id),
                sender_kind=PLATFORM_KIND,
                account_id=account,
                action_id=action.id,
                outcome=outcome,
                content_source=source,
                content_preview=preview,
                observed_at=model_now(),
            )
        )
    session.flush()


def record_conversation_event(
    session: Session,
    *,
    tenant_id: int,
    surface: str,
    conversation_key: str,
    remote_message_id: str,
    sender_kind: str,
    remote_cursor: str = "",
    account_id: int | None = None,
    action_id: str | None = None,
    outcome: str = "observed",
    content_source: str = "",
    observed_at: datetime | None = None,
) -> ConversationSpeakerTurn | None:
    remote_id = str(remote_message_id or "").strip()
    if not remote_id:
        return None
    existing = session.scalar(
        select(ConversationSpeakerTurn).where(
            ConversationSpeakerTurn.tenant_id == tenant_id,
            ConversationSpeakerTurn.surface == surface,
            ConversationSpeakerTurn.conversation_key == conversation_key,
            ConversationSpeakerTurn.remote_message_id == remote_id,
        )
    )
    if existing is not None:
        return existing
    turn = ConversationSpeakerTurn(
        tenant_id=tenant_id,
        surface=surface,
        conversation_key=conversation_key,
        remote_message_id=remote_id,
        remote_cursor=str(remote_cursor or remote_id),
        sender_kind=sender_kind,
        account_id=account_id,
        action_id=action_id,
        outcome=outcome,
        content_source=content_source,
        observed_at=observed_at or model_now(),
    )
    session.add(turn)
    if sender_kind == HUMAN_KIND:
        state = lock_or_create_state(
            session,
            tenant_id=tenant_id,
            surface=surface,
            conversation_key=conversation_key,
        )
        state.last_human_cursor = str(remote_cursor or remote_id)
        state.version = int(state.version or 1) + 1
    session.flush()
    return turn


def last_platform_content_source(
    session: Session,
    *,
    tenant_id: int,
    surface: str,
    conversation_key: str,
) -> str:
    state = session.scalar(
        select(ConversationSpeakerState).where(
            ConversationSpeakerState.tenant_id == tenant_id,
            ConversationSpeakerState.surface == surface,
            ConversationSpeakerState.conversation_key == conversation_key,
        )
    )
    return str(state.last_platform_content_source or "") if state else ""


def last_platform_text(
    session: Session,
    *,
    tenant_id: int,
    surface: str,
    conversation_key: str,
) -> str:
    turn = session.scalar(
        select(ConversationSpeakerTurn)
        .where(
            ConversationSpeakerTurn.tenant_id == tenant_id,
            ConversationSpeakerTurn.surface == surface,
            ConversationSpeakerTurn.conversation_key == conversation_key,
            ConversationSpeakerTurn.sender_kind == PLATFORM_KIND,
        )
        .order_by(ConversationSpeakerTurn.observed_at.desc(), ConversationSpeakerTurn.id.desc())
        .limit(1)
    )
    return str(turn.content_preview or "").strip() if turn else ""


__all__ = [
    "SpeakerDecision",
    "conversation_key_for_group",
    "conversation_key_for_discussion",
    "lock_or_create_state",
    "human_has_broken_adjacency",
    "reserve_speaker_turn",
    "release_speaker_reservation",
    "finalize_speaker_turn",
    "record_conversation_event",
    "last_platform_content_source",
    "last_platform_text",
    "PLATFORM_HOLDS",
    "HUMAN_KIND",
    "PLATFORM_KIND",
    "CONTROL_KIND",
    "SYSTEM_KIND",
]
