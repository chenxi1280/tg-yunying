from __future__ import annotations

from datetime import datetime, timedelta
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    ContextTurn,
    ConversationEvent,
    ConversationTurnClaim,
    GroupContextMessage,
    InteractionOpportunity,
    Task,
    Tenant,
    TgGroup,
)
from app.services.task_center.engagement_conversation import (
    _append_event_to_turn,
    materialize_due_turns,
    project_group_context_message,
)

pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as sess:
        yield sess


def test_different_authors_not_merged_into_same_turn(session):
    tenant = Tenant(id=1, name="Test Tenant")
    group = TgGroup(id=1, tenant_id=1, tg_peer_id="-100999", title="Test Group")
    session.add_all([tenant, group])
    session.flush()

    base_time = datetime(2026, 9, 4, 12, 0, 0)

    # User A sends message
    msg_a = GroupContextMessage(
        id=1,
        tenant_id=1,
        group_id=group.id,
        remote_message_id="101",
        sender_peer_id="user_a",
        sender_name="Alice",
        content="Hello from Alice",
        sent_at=base_time,
    )
    # User B sends message 1 second later
    msg_b = GroupContextMessage(
        id=2,
        tenant_id=1,
        group_id=group.id,
        remote_message_id="102",
        sender_peer_id="user_b",
        sender_name="Bob",
        content="Hello from Bob",
        sent_at=base_time + timedelta(seconds=1),
    )

    event_a = project_group_context_message(session, group, msg_a)
    event_b = project_group_context_message(session, group, msg_b)

    turns = list(session.scalars(select(ContextTurn).order_by(ContextTurn.first_event_at)))
    assert len(turns) == 2
    assert turns[0].author_peer_id == "user_a"
    assert turns[1].author_peer_id == "user_b"
    assert turns[0].event_ids == [event_a.id]
    assert turns[1].event_ids == [event_b.id]


def test_terminal_punctuation_adaptive_window(session):
    tenant = Tenant(id=1, name="Test Tenant")
    group = TgGroup(id=1, tenant_id=1, tg_peer_id="-100999", title="Test Group")
    session.add_all([tenant, group])
    session.flush()

    base_time = datetime(2026, 9, 4, 12, 0, 0)

    # Message with question mark -> terminal sentence -> 2.5s idle
    msg_q = GroupContextMessage(
        id=1,
        tenant_id=1,
        group_id=group.id,
        remote_message_id="201",
        sender_peer_id="user_a",
        content="请问今天开会吗？",
        sent_at=base_time,
    )
    event_q = project_group_context_message(session, group, msg_q)
    turn_q = session.scalar(select(ContextTurn).where(ContextTurn.author_peer_id == "user_a"))
    assert (turn_q.closed_at - turn_q.first_event_at).total_seconds() == pytest.approx(2.5)

    # Message without terminal punctuation -> incomplete -> 5.0s idle
    msg_c = GroupContextMessage(
        id=2,
        tenant_id=1,
        group_id=group.id,
        remote_message_id="202",
        sender_peer_id="user_b",
        content="我想了解一下关于",
        sent_at=base_time,
    )
    event_c = project_group_context_message(session, group, msg_c)
    turn_c = session.scalar(select(ContextTurn).where(ContextTurn.author_peer_id == "user_b"))
    assert (turn_c.closed_at - turn_c.first_event_at).total_seconds() == pytest.approx(5.0)


def test_hard_cap_12_seconds(session):
    tenant = Tenant(id=1, name="Test Tenant")
    group = TgGroup(id=1, tenant_id=1, tg_peer_id="-100999", title="Test Group")
    session.add_all([tenant, group])
    session.flush()

    base_time = datetime(2026, 9, 4, 12, 0, 0)

    # Event 1 at T=0s
    msg1 = GroupContextMessage(
        id=1,
        tenant_id=1,
        group_id=group.id,
        remote_message_id="301",
        sender_peer_id="user_a",
        content="frag 1",
        sent_at=base_time,
    )
    project_group_context_message(session, group, msg1)

    # Event 2 at T=4s
    msg2 = GroupContextMessage(
        id=2,
        tenant_id=1,
        group_id=group.id,
        remote_message_id="302",
        sender_peer_id="user_a",
        content="frag 2",
        sent_at=base_time + timedelta(seconds=4),
    )
    project_group_context_message(session, group, msg2)

    # Event 3 at T=8s
    msg3 = GroupContextMessage(
        id=3,
        tenant_id=1,
        group_id=group.id,
        remote_message_id="303",
        sender_peer_id="user_a",
        content="frag 3",
        sent_at=base_time + timedelta(seconds=8),
    )
    project_group_context_message(session, group, msg3)

    turn = session.scalar(select(ContextTurn).where(ContextTurn.author_peer_id == "user_a"))
    # closed_at must be clamped by hard_cap = 12.0s from first_event_at
    assert (turn.closed_at - turn.first_event_at).total_seconds() <= 12.0
