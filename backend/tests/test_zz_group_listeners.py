from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.gateways import GroupMessageSnapshot
from app.main import app
from app.models import AccountStatus, AppUser, Campaign, GroupContextMessage, MessageTask, TgGroup, TgGroupAccount
from app.models.enums import now
from app.services.group_listeners import process_group_listener
from app.worker import drain_once
from tests.test_workflow import auth_headers, ensure_developer_app, ensure_test_workspace


def _active_account(client: TestClient, headers: dict[str, str], display_name: str) -> dict:
    account = client.post(
        "/api/tg-accounts",
        headers=headers,
        json={
            "tenant_id": 1,
            "display_name": display_name,
            "username": f"listener_{uuid4().hex[:8]}",
            "phone_number": f"+86139{int(uuid4().int % 100000000):08d}",
        },
    ).json()
    if account["status"] != AccountStatus.ACTIVE.value:
        client.post(f"/api/tg-accounts/{account['id']}/login/start", headers=headers, json={"method": "qr"})
        account = client.post(f"/api/tg-accounts/{account['id']}/login/qr/check", headers=headers).json()
    client.post(f"/api/tg-accounts/{account['id']}/sync-groups", headers=headers)
    return account


def _expire_tenant_subscription(tenant_id: int = 1) -> tuple[int, str, object, object]:
    with SessionLocal() as session:
        user = session.query(AppUser).filter(AppUser.tenant_id == tenant_id, AppUser.role == "普通用户").order_by(AppUser.id.asc()).first()
        assert user is not None
        original = (user.id, user.subscription_status, user.subscription_started_at, user.subscription_expires_at)
        user.subscription_status = "expired"
        user.subscription_expires_at = now() - timedelta(days=1)
        session.commit()
        return original


def _restore_tenant_subscription(original: tuple[int, str, object, object]) -> None:
    user_id, status, started_at, expires_at = original
    with SessionLocal() as session:
        user = session.get(AppUser, user_id)
        assert user is not None
        user.subscription_status = status
        user.subscription_started_at = started_at
        user.subscription_expires_at = expires_at
        session.commit()


def test_group_listener_config_rejects_invalid_account():
    with TestClient(app) as client:
        headers = auth_headers(client)
        _, group = ensure_test_workspace(client, headers)

        response = client.patch(
            f"/api/groups/{group['id']}",
            headers=headers,
            json={"listener_enabled": True, "listener_account_ids": [999999]},
        )

        assert response.status_code == 404


def test_group_listener_collects_context_and_auto_queues_reply(monkeypatch):
    with TestClient(app) as client:
        headers = auth_headers(client)
        ensure_developer_app(client, headers)
        listener, group = ensure_test_workspace(client, headers)
        sender = _active_account(client, headers, "自动续聊发送号")

        provider = client.post(
            "/api/ai-providers",
            headers=headers,
            json={
                "provider_name": "Listener Mock",
                "provider_type": "openai_compatible",
                "base_url": "mock://openai-compatible",
                "model_name": "deepseek-v4-flash",
                "api_key": "mock_listener_key",
            },
        ).json()
        client.patch(
            "/api/tenant-ai-settings?tenant_id=1",
            headers=headers,
            json={"default_provider_id": provider["id"], "ai_enabled": True, "fallback_to_mock": False},
        )

        with SessionLocal() as session:
            for link in session.query(TgGroupAccount).filter_by(group_id=group["id"]):
                link.can_send = link.account_id in {listener["id"], sender["id"]}
            session.commit()

        snapshots = [
            GroupMessageSnapshot(
                remote_message_id="remote-real-1",
                sender_peer_id="real-user-1",
                sender_name="真人用户",
                content="这个功能怎么开始参与？",
            ),
            GroupMessageSnapshot(
                remote_message_id="remote-managed-1",
                sender_peer_id=f"account:{sender['id']}",
                sender_name=sender["display_name"],
                content="托管账号自己发的消息不应触发。",
            ),
        ]
        monkeypatch.setattr("app.services.group_listeners.gateway.fetch_group_messages", lambda *args, **kwargs: snapshots)

        patched = client.patch(
            f"/api/groups/{group['id']}",
            headers=headers,
            json={
                "listener_enabled": True,
                "listener_auto_reply_enabled": True,
                "listener_interval_seconds": 30,
                "listener_context_limit": 20,
                "listener_account_ids": [listener["id"]],
            },
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["listener_account_ids"] == [listener["id"]]

        with SessionLocal() as session:
            for link in session.query(TgGroupAccount).filter_by(group_id=group["id"]):
                link.is_listener = link.account_id == listener["id"]
                link.can_send = link.account_id == sender["id"]
            db_group = session.get(TgGroup, group["id"])
            db_group.listener_last_polled_at = None
            session.commit()

        processed = drain_once()
        assert processed >= 2

        with SessionLocal() as session:
            contexts = session.query(GroupContextMessage).filter_by(group_id=group["id"], remote_message_id="remote-real-1").all()
            assert len(contexts) == 1
            assert contexts[0].content == "这个功能怎么开始参与？"
            assert contexts[0].used_for_ai is True
            tasks = session.query(MessageTask).filter_by(group_id=group["id"]).order_by(MessageTask.id.desc()).limit(5).all()
            assert tasks
            assert tasks[0].preferred_account_id == sender["id"]

        drain_once()
        assert drain_once() == 0


def test_group_listener_auto_reply_skips_when_subscription_expired(monkeypatch):
    with TestClient(app) as client:
        headers = auth_headers(client)
        ensure_developer_app(client, headers)
        listener, group = ensure_test_workspace(client, headers)

        snapshots = [
            GroupMessageSnapshot(
                remote_message_id=f"expired-real-{uuid4().hex[:8]}",
                sender_peer_id="real-expired-user",
                sender_name="真人用户",
                content="订阅过期时不应自动回复。",
            )
        ]
        monkeypatch.setattr("app.services.group_listeners.gateway.fetch_group_messages", lambda *args, **kwargs: snapshots)

        patched = client.patch(
            f"/api/groups/{group['id']}",
            headers=headers,
            json={
                "listener_enabled": True,
                "listener_auto_reply_enabled": True,
                "listener_interval_seconds": 30,
                "listener_context_limit": 20,
                "listener_account_ids": [listener["id"]],
            },
        )
        assert patched.status_code == 200, patched.text

        with SessionLocal() as session:
            db_group = session.get(TgGroup, group["id"])
            db_group.listener_last_polled_at = None
            campaign_count = session.query(Campaign).count()
            task_count = session.query(MessageTask).count()
            session.commit()

        original = _expire_tenant_subscription()
        try:
            with SessionLocal() as session:
                assert process_group_listener(session, group["id"]) == 1

            with SessionLocal() as session:
                db_group = session.get(TgGroup, group["id"])
                contexts = session.query(GroupContextMessage).filter_by(remote_message_id=snapshots[0].remote_message_id).all()
                assert db_group.listener_last_error == "subscription inactive"
                assert len(contexts) == 1
                assert contexts[0].used_for_ai is False
                assert session.query(Campaign).count() == campaign_count
                assert session.query(MessageTask).count() == task_count
        finally:
            _restore_tenant_subscription(original)
