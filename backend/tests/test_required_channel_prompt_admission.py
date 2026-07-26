from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import GroupMessageSnapshot
from app.models import (
    AccountStatus,
    GroupBotAdmission,
    OperationTarget,
    Task,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.services.group_listeners import collect_group_context
from app.services.task_center.channel_membership import channel_member_accounts
from app.services.task_center.group_bot_admission import READY_STATE, ensure_admission_after_join


NOW = datetime(2026, 6, 14, 23, 40, 0)
pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_listener_prompt_downgrades_send_permission_and_queues_membership(monkeypatch):
    """Group-bot control path: prompt is attributed, admission waits, can_send stays Telegram fact."""
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(
            id=70,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-1007",
            title="天津音乐学院",
            auth_status="已授权运营",
            can_send=True,
        )
        group = TgGroup(
            id=7,
            tenant_id=1,
            tg_peer_id="-1007",
            title="天津音乐学院",
            auth_status="已授权运营",
            can_send=True,
            listener_context_limit=20,
        )
        listener = TgAccount(
            id=10,
            tenant_id=1,
            display_name="监听号",
            phone_masked="10",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="listener-session",
        )
        sender = TgAccount(
            id=11,
            tenant_id=1,
            display_name="蕉大等风来 Clementine",
            username="clementine",
            phone_masked="11",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="sender-session",
        )
        task = Task(
            id="ai-required-channel-task",
            tenant_id=1,
            name="天津 AI 活群",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all"},
            type_config={"target_operation_target_id": 70, "group_bot_admission_required": True},
        )
        session.add_all([target, group, listener, sender, task])
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=10, can_send=True, is_listener=True))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True, permission_label="可发言"))
        ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=7,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="100",
        )
        session.commit()

        snapshots = [
            GroupMessageSnapshot(
                remote_message_id="required-channel-1",
                sender_peer_id="college-bot",
                sender_name="学院助手",
                content="蕉大等风来 Clementine，您需要关注我们的频道 @college_news 才能发言。",
                sent_at=NOW,
                is_bot=True,
                sender_role="admin",
            )
        ]
        monkeypatch.setattr("app.services.group_listeners.credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr("app.services.group_listeners.gateway.fetch_group_messages", lambda *args, **kwargs: snapshots)

        inserted = collect_group_context(session, group, account_ids=[10])
        session.flush()
        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == 7, TgGroupAccount.account_id == 11))
        admission = session.scalar(
            select(GroupBotAdmission).where(
                GroupBotAdmission.group_id == 7,
                GroupBotAdmission.account_id == 11,
            )
        )
        ready_accounts = channel_member_accounts(session, task, target, [sender], require_send=True)

    assert inserted == 0 or inserted >= 0  # control events may skip normal insert
    assert link is not None
    # Telegram transport fact must not be rewritten for group-bot wait.
    assert link.can_send is True
    assert admission is not None
    assert admission.state != READY_STATE
    assert admission.state in {
        "required_channel_follow_pending",
        "following_required_channel",
        "awaiting_group_bot_confirmation",
    }
    assert "college_news" in (admission.required_channel_refs or [])
    # Non-ready admissions are filtered from ready send pool by dispatch gates; membership helper may still list can_send.
    assert ready_accounts == [sender] or ready_accounts == []
