from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import GroupMessageSnapshot
from app.models import Action, AccountStatus, OperationTarget, Task, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.services.group_listeners import collect_group_context
from app.services.task_center.channel_membership import channel_member_accounts


NOW = datetime(2026, 6, 14, 23, 40, 0)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_listener_prompt_downgrades_send_permission_and_queues_membership(monkeypatch):
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
            type_config={"target_operation_target_id": 70, "auto_follow_required_channel": True},
        )
        session.add_all([target, group, listener, sender, task])
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=10, can_send=True, is_listener=True))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True, permission_label="可发言"))
        session.commit()

        snapshots = [
            GroupMessageSnapshot(
                remote_message_id="required-channel-1",
                sender_peer_id="college-bot",
                sender_name="学院助手",
                content="蕉大等风来 Clementine，您需要关注我们的频道才能发言。",
                sent_at=NOW,
                is_bot=True,
            )
        ]
        monkeypatch.setattr("app.services.group_listeners.credentials_for_account", lambda *args, **kwargs: object())
        monkeypatch.setattr("app.services.group_listeners.gateway.fetch_group_messages", lambda *args, **kwargs: snapshots)

        inserted = collect_group_context(session, group, account_ids=[10])
        session.flush()
        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == 7, TgGroupAccount.account_id == 11))
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id, Action.account_id == 11)))
        ready_accounts = channel_member_accounts(session, task, target, [sender], require_send=True)

    assert inserted == 1
    assert link is not None
    assert link.can_send is False
    assert "待关注必需频道" in link.permission_label
    assert [action.action_type for action in actions] == ["ensure_target_membership"]
    assert actions[0].status == "pending"
    assert actions[0].payload["require_send"] is True
    assert actions[0].result["reactivated_reason"] == "required_channel_prompt_detected"
    assert ready_accounts == []
