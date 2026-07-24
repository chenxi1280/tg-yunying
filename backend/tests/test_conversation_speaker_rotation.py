from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, Tenant
from app.services.task_center.conversation_speaker_rotation import (
    conversation_key_for_group,
    finalize_speaker_turn,
    human_has_broken_adjacency,
    lock_or_create_state,
    record_conversation_event,
    reserve_speaker_turn,
)

pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _action(session: Session, *, account_id: int = 101, action_id: str = "a1") -> Action:
    action = Action(
        id=action_id,
        tenant_id=1,
        task_id="task-1",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=account_id,
        status="pending",
        payload={"group_id": 7},
        result={},
    )
    session.add(action)
    session.flush()
    return action


def test_reserve_next_speaker_rotates_when_another_eligible_account_exists():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        first = _action(session, account_id=101, action_id="a-first")
        key = conversation_key_for_group(group_id=7)
        reserve_speaker_turn(
            session,
            action=first,
            surface="group_ai_chat",
            conversation_key=key,
            candidate_account_ids=[101],
        )
        finalize_speaker_turn(
            session,
            action=first,
            surface="group_ai_chat",
            conversation_key=key,
            outcome="success",
            remote_message_id="m1",
            account_id=101,
        )
        second = _action(session, account_id=101, action_id="a-second")
        decision = reserve_speaker_turn(
            session,
            action=second,
            surface="group_ai_chat",
            conversation_key=key,
            candidate_account_ids=[101, 102],
        )
        assert decision.allowed is True
        assert decision.account_id == 102
        assert decision.reason == "rotated_from_last_speaker"


def test_single_candidate_is_deferred_instead_of_silent_same_account_repeat():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        first = _action(session, account_id=101, action_id="a1")
        key = conversation_key_for_group(group_id=7)
        reserve_speaker_turn(
            session,
            action=first,
            surface="group_ai_chat",
            conversation_key=key,
            candidate_account_ids=[101],
        )
        finalize_speaker_turn(
            session,
            action=first,
            surface="group_ai_chat",
            conversation_key=key,
            outcome="success",
            remote_message_id="m1",
            account_id=101,
        )
        second = _action(session, account_id=101, action_id="a2")
        decision = reserve_speaker_turn(
            session,
            action=second,
            surface="group_ai_chat",
            conversation_key=key,
            candidate_account_ids=[101],
        )
        assert decision.allowed is False
        assert decision.code == "speaker_rotation_wait"


def test_real_human_message_breaks_same_account_adjacency_but_bot_control_does_not():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        first = _action(session, account_id=101, action_id="a1")
        key = conversation_key_for_group(group_id=7)
        reserve_speaker_turn(
            session,
            action=first,
            surface="group_ai_chat",
            conversation_key=key,
            candidate_account_ids=[101],
        )
        finalize_speaker_turn(
            session,
            action=first,
            surface="group_ai_chat",
            conversation_key=key,
            outcome="success",
            remote_message_id="m1",
            account_id=101,
        )
        record_conversation_event(
            session,
            tenant_id=1,
            surface="group_ai_chat",
            conversation_key=key,
            remote_message_id="20",
            sender_kind="group_bot_control",
        )
        blocked = reserve_speaker_turn(
            session,
            action=_action(session, account_id=101, action_id="a2"),
            surface="group_ai_chat",
            conversation_key=key,
            candidate_account_ids=[101],
        )
        assert blocked.code == "speaker_rotation_wait"
        record_conversation_event(
            session,
            tenant_id=1,
            surface="group_ai_chat",
            conversation_key=key,
            remote_message_id="21",
            sender_kind="human",
        )
        state = lock_or_create_state(session, tenant_id=1, surface="group_ai_chat", conversation_key=key)
        assert human_has_broken_adjacency(session, state) is True
        allowed = reserve_speaker_turn(
            session,
            action=_action(session, account_id=101, action_id="a3"),
            surface="group_ai_chat",
            conversation_key=key,
            candidate_account_ids=[101],
        )
        assert allowed.allowed is True
        assert allowed.account_id == 101
