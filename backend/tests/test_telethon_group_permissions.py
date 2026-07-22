import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.integrations.telegram import DeveloperAppCredentials, TelethonTelegramGateway
from app.integrations.telegram.gateway import _can_send_text_in_group


pytestmark = pytest.mark.no_postgres


def test_regular_supergroup_member_can_send_when_default_text_is_not_banned():
    target = SimpleNamespace(
        default_banned_rights=SimpleNamespace(send_messages=False, send_plain=False),
    )
    permissions = SimpleNamespace(
        is_admin=False,
        is_creator=False,
        post_messages=False,
        participant=SimpleNamespace(),
    )

    assert _can_send_text_in_group(target, permissions) is True


def test_supergroup_member_cannot_send_when_default_text_is_banned():
    target = SimpleNamespace(
        default_banned_rights=SimpleNamespace(send_messages=True, send_plain=False),
    )
    permissions = SimpleNamespace(
        is_admin=False,
        is_creator=False,
        post_messages=False,
        participant=SimpleNamespace(),
    )

    assert _can_send_text_in_group(target, permissions) is False


def test_public_group_resolution_does_not_enumerate_dialogs(monkeypatch):
    from telethon import types

    class FakeClient:
        def __init__(self):
            self.calls: list[tuple[str, object]] = []

        async def get_entity(self, username):
            self.calls.append(("get_entity", username))
            return types.Channel(
                id=123,
                title="郑州楼凤",
                photo=None,
                date=None,
                megagroup=True,
                access_hash=1,
                username="zhengzhou167",
                participants_count=31144,
            )

        async def get_permissions(self, entity, user):
            self.calls.append(("get_permissions", user))
            return SimpleNamespace(
                is_admin=False,
                is_creator=False,
                post_messages=False,
                participant=SimpleNamespace(),
                send_messages=True,
            )

    gateway = TelethonTelegramGateway(Settings(telethon_operation_timeout_seconds=1))
    client = FakeClient()

    async def authorized_client(*_args, **_kwargs):
        return client

    monkeypatch.setattr(gateway, "_authorized_client", authorized_client)
    monkeypatch.setattr(gateway, "_run", lambda coroutine: asyncio.run(coroutine))
    snapshot = gateway.resolve_group_by_public_username(
        11,
        "@zhengzhou167",
        "session",
        DeveloperAppCredentials(app_id=1, api_id=1, api_hash="hash", credentials_version=1),
    )

    assert snapshot.username == "zhengzhou167"
    assert snapshot.can_send is True
    assert [call[0] for call in client.calls] == ["get_entity", "get_permissions"]
