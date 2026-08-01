from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ConversationSpeakerTurn,
    ExecutionAttempt,
    GroupContextMessage,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services.group_listener_context_writer import insert_context_snapshots


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _scope(session: Session) -> tuple[TgGroup, TgAccount]:
    session.add(Tenant(id=1, name="tenant"))
    group = TgGroup(
        id=7,
        tenant_id=1,
        tg_peer_id="-1007",
        title="group",
        group_type="supergroup",
    )
    account = TgAccount(
        id=11,
        tenant_id=1,
        display_name="listener",
        username="listener",
        phone_masked="+100",
    )
    session.add_all([group, account])
    session.flush()
    return group, account


def test_managed_snapshot_is_platform_turn_and_not_human_context() -> None:
    with _session() as session:
        group, account = _scope(session)
        snapshot = SimpleNamespace(
            content="platform message",
            remote_message_id="9001",
            sender_peer_id="unmatched-peer",
            sender_name="unmatched-name",
            sender_username="",
            is_bot=False,
            sender_role="member",
            message_type="text",
            sent_at=None,
        )

        inserted = insert_context_snapshots(
            session,
            group,
            account,
            [snapshot],
            ignored_sender=lambda _snapshot: True,
            create_source_media=False,
            learning_scene=None,
        )

        turn = session.scalar(select(ConversationSpeakerTurn))
        assert inserted == 0
        assert session.scalar(select(GroupContextMessage.id)) is None
        assert turn is not None
        assert turn.sender_kind == "platform"


def test_outbound_remote_fact_identifies_platform_when_profile_does_not() -> None:
    from app.services.group_listener_sender_identity import (
        outbound_remote_ids_for_snapshots,
    )

    with _session() as session:
        group, account = _scope(session)
        action = Action(
            id="send-1",
            tenant_id=1,
            task_id="task-1",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=account.id,
            status="success",
            payload={"group_id": group.id},
        )
        session.add(action)
        session.flush()
        session.add(
            ExecutionAttempt(
                id="attempt-1",
                tenant_id=1,
                action_id=action.id,
                account_id=account.id,
                status="success",
                remote_message_id="9001",
            )
        )
        session.flush()
        snapshots = [SimpleNamespace(remote_message_id="9001")]

        assert outbound_remote_ids_for_snapshots(session, group, snapshots) == {"9001"}
