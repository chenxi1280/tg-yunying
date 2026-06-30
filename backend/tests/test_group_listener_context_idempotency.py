from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import GroupMessageSnapshot
from app.models import AccountStatus, GroupContextMessage, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.services.group_listeners import collect_group_context


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_collect_group_context_ignores_duplicate_unique_race(monkeypatch):
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=10, tenant_id=1, tg_peer_id="-10010", title="活群", listener_enabled=True)
        listener = TgAccount(
            id=20,
            tenant_id=1,
            phone_masked="+10000000020",
            display_name="监听号",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="listener-session",
        )
        session.add_all([group, listener])
        session.add(TgGroupAccount(id=30, tenant_id=1, group_id=10, account_id=20, is_listener=True))
        session.add(
            GroupContextMessage(
                tenant_id=1,
                group_id=10,
                listener_account_id=20,
                sender_peer_id="real-user",
                sender_name="真人用户",
                content="已经入库的消息",
                remote_message_id="race-message-1",
            )
        )
        session.commit()

        snapshots = [
            GroupMessageSnapshot(
                remote_message_id="race-message-1",
                sender_peer_id="real-user",
                sender_name="真人用户",
                content="已经入库的消息",
            )
        ]
        monkeypatch.setattr("app.services.group_listeners.credentials_for_account", lambda *args, **kwargs: {})
        monkeypatch.setattr("app.services.group_listeners.gateway.fetch_group_messages", lambda *args, **kwargs: snapshots)
        original_scalar = session.scalar

        def miss_context_exists(statement, *args, **kwargs):
            text = str(statement)
            if "group_context_messages" in text and "remote_message_id" in text:
                return None
            return original_scalar(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalar", miss_context_exists)

        inserted = collect_group_context(session, group, account_ids=[20], learning_scene=None)
        context_count = session.query(GroupContextMessage).filter_by(group_id=10, remote_message_id="race-message-1").count()

    assert inserted == 0
    assert context_count == 1
