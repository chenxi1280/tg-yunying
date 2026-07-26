from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import GroupBotAdmission, GroupContextMessage, Tenant, TgAccount, TgGroup
from app.services.group_listener_context_writer import insert_context_snapshots
from app.services.task_center.group_bot_admission import ensure_admission_after_join, READY_STATE
from app.services.task_center.payloads import GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE

pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_listener_control_event_ingests_trusted_bot_before_context():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        group = TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g", group_type="supergroup")
        account = TgAccount(
            id=11,
            tenant_id=1,
            display_name="clementine",
            username="clementine",
            phone_masked="+100",
        )
        session.add_all([group, account])
        session.flush()
        ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=7,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="10",
        )
        snapshot = SimpleNamespace(
            content="@clementine 请先关注 https://t.me/school_news",
            remote_message_id="99",
            sender_peer_id="900",
            sender_name="gatebot",
            sender_username="gatebot",
            is_bot=True,
            sender_role="admin",
            message_type="text",
            sent_at=None,
        )
        inserted = insert_context_snapshots(
            session,
            group,
            account,
            [snapshot],
            ignored_sender=lambda _s: False,
            create_source_media=False,
            learning_scene=None,
        )
        # Control bot rules are persisted only as bot audit context; AI readers filter is_bot.
        assert inserted == 1
        context = session.get(GroupContextMessage, 1)
        assert context is not None
        assert context.is_bot is True
        admission = session.scalar(
            select_admission(session, group_id=7, account_id=11)
        )
        assert admission is not None
        assert admission.state == "required_channel_follow_pending"
        assert "school_news" in (admission.required_channel_refs or [])


def select_admission(session: Session, *, group_id: int, account_id: int):
    from sqlalchemy import select

    return select(GroupBotAdmission).where(
        GroupBotAdmission.group_id == group_id,
        GroupBotAdmission.account_id == account_id,
    )


def test_dispatch_fairness_classifies_group_bot_follow_as_admission_retry():
    from app.services.task_center.dispatch_fairness import classify_action_payload

    # Unbound follow actions stay ordinary so they cannot starve search_join globally.
    assert classify_action_payload(GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE, {}, "group_ai_chat") == "ordinary"
    # Bound to same tenant+task+account admission → target_admission_retry tier (PRD §8.3).
    bound = {
        "admission_bound_task_id": "task-1",
        "admission_bound_account_id": 11,
    }
    assert (
        classify_action_payload(GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE, bound, "group_ai_chat")
        == "target_admission_retry"
    )
    assert (
        classify_action_payload("group_bot_control_observation", bound, "group_ai_chat")
        == "target_admission_retry"
    )
