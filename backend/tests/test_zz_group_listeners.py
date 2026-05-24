from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.integrations.telegram import GroupMessageSnapshot
from app.main import app
from app.models import AccountStatus, Campaign, GroupContextMessage, MessageTask, SourceMediaAsset, TgGroup, TgGroupAccount
from app.services.group_listeners import process_group_listener
from tests.test_workflow import _next_test_phone, auth_headers, ensure_developer_app, ensure_test_workspace


def _active_account(client: TestClient, headers: dict[str, str], display_name: str) -> dict:
    for _ in range(20):
        response = client.post(
            "/api/tg-accounts",
            headers=headers,
            json={
                "tenant_id": 1,
                "display_name": display_name,
                "username": f"listener_{uuid4().hex[:8]}",
                "phone_number": _next_test_phone("+8613900"),
            },
        )
        if response.status_code == 200:
            break
        assert "手机号已存在" in response.text, response.text
    assert response.status_code == 200, response.text
    account = response.json()
    if account["status"] != AccountStatus.ACTIVE.value:
        client.post(f"/api/tg-accounts/{account['id']}/login/start", headers=headers, json={"method": "qr"})
        account = client.post(f"/api/tg-accounts/{account['id']}/login/qr/check", headers=headers).json()
    client.post(f"/api/tg-accounts/{account['id']}/sync-groups", headers=headers)
    return account


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


def test_group_listener_collects_context_without_legacy_auto_reply(monkeypatch):
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
                remote_message_id="remote-bot-1",
                sender_peer_id="bot-user-1",
                sender_name="群机器人",
                content="机器人公告不应触发转发或续聊。",
                is_bot=True,
            ),
            GroupMessageSnapshot(
                remote_message_id="remote-managed-1",
                sender_peer_id=f"account:{sender['id']}",
                sender_name=sender["display_name"],
                content="托管账号自己发的消息不应触发。",
            ),
            GroupMessageSnapshot(
                remote_message_id="remote-source-channel-1",
                sender_peer_id=group["tg_peer_id"],
                sender_name=group["title"],
                content="来源频道机器发送的消息不应触发转发。",
            ),
            GroupMessageSnapshot(
                remote_message_id="remote-source-channel-typed-1",
                sender_peer_id=group["tg_peer_id"].removeprefix("-100"),
                sender_name=group["title"],
                sender_peer_type="channel",
                content="来源频道裸数字 sender id 也不应触发转发。",
            ),
            GroupMessageSnapshot(
                remote_message_id="remote-media-1",
                sender_peer_id="real-user-media",
                sender_name="真人用户",
                content="[media]",
                message_type="media",
                caption="相册第一张",
                media_type="photo",
                media_fingerprint="media-fingerprint-1",
                media_group_id="album-1",
                media_group_index=1,
                media_group_total=2,
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

        with SessionLocal() as session:
            processed = process_group_listener(session, group["id"])
        assert processed >= 1

        with SessionLocal() as session:
            contexts = session.query(GroupContextMessage).filter_by(group_id=group["id"], remote_message_id="remote-real-1").all()
            assert len(contexts) == 1
            assert contexts[0].content == "这个功能怎么开始参与？"
            assert contexts[0].used_for_ai is False
            media_context = session.query(GroupContextMessage).filter_by(group_id=group["id"], remote_message_id="remote-media-1").one()
            assert media_context.message_type == "media"
            media_asset = session.query(SourceMediaAsset).filter_by(source_group_id=group["id"], source_message_id="remote-media-1").one()
            assert media_asset.source_media_group_id == "album-1"
            assert media_asset.media_group_index == 1
            assert media_asset.media_group_total == 2
            assert media_asset.cache_status == "pending_cache"
            bot_context = session.query(GroupContextMessage).filter_by(group_id=group["id"], remote_message_id="remote-bot-1").one()
            assert bot_context.is_bot is True
            assert bot_context.sender_name == "群机器人"
            assert session.query(GroupContextMessage).filter_by(group_id=group["id"], remote_message_id="remote-source-channel-1").count() == 0
            assert session.query(GroupContextMessage).filter_by(group_id=group["id"], remote_message_id="remote-source-channel-typed-1").count() == 0
            tasks = session.query(MessageTask).filter_by(group_id=group["id"]).order_by(MessageTask.id.desc()).limit(5).all()
            assert not any(task.preferred_account_id == sender["id"] and task.content == "这个功能怎么开始参与？" for task in tasks)

        with SessionLocal() as session:
            process_group_listener(session, group["id"])
            process_group_listener(session, group["id"])
        with SessionLocal() as session:
            assert session.query(GroupContextMessage).filter_by(group_id=group["id"], remote_message_id="remote-real-1").count() == 1
            assert session.query(SourceMediaAsset).filter_by(source_group_id=group["id"], source_message_id="remote-media-1").count() == 1


def test_group_listener_context_collection_is_not_subscription_gated(monkeypatch):
    with TestClient(app) as client:
        headers = auth_headers(client)
        ensure_developer_app(client, headers)
        listener, group = ensure_test_workspace(client, headers)

        snapshots = [
            GroupMessageSnapshot(
                remote_message_id=f"expired-real-{uuid4().hex[:8]}",
                sender_peer_id="real-expired-user",
                sender_name="真人用户",
                content="没有订阅体系后也应自动回复。",
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
            for link in session.query(TgGroupAccount).filter_by(group_id=group["id"]):
                link.can_send = link.account_id == listener["id"]
            db_group = session.get(TgGroup, group["id"])
            db_group.listener_last_polled_at = None
            campaign_count = session.query(Campaign).count()
            task_count = session.query(MessageTask).count()
            session.commit()

        with SessionLocal() as session:
            assert process_group_listener(session, group["id"]) >= 1

        with SessionLocal() as session:
            db_group = session.get(TgGroup, group["id"])
            contexts = session.query(GroupContextMessage).filter_by(remote_message_id=snapshots[0].remote_message_id).all()
            assert db_group.listener_last_error == ""
            assert len(contexts) == 1
            assert contexts[0].used_for_ai is False
            assert session.query(Campaign).count() == campaign_count
            assert session.query(MessageTask).count() == task_count
