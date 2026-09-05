from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import UserAlreadyParticipantError

from app.integrations.telegram.contracts import DeveloperAppCredentials
from app.integrations.telegram.gateway import TelethonTelegramGateway


pytestmark = [pytest.mark.no_postgres, pytest.mark.anyio]


@pytest.fixture
def credentials():
    return DeveloperAppCredentials(
        app_id=1, api_id=12345, api_hash="test", credentials_version=1,
        app_name="test",
    )


async def invoke(client, credentials, *, target="channel", raw_session="test", connect_error=None):
    gateway = TelethonTelegramGateway()
    with patch.object(gateway, "_get_or_create_client", return_value=client, side_effect=connect_error), patch(
        "app.integrations.telegram.gateway.decrypt_session", return_value=raw_session
    ):
        return await gateway._ensure_channel_membership_async("encrypted", target, credentials)


@pytest.mark.parametrize("phase", ["session", "connect", "authorization", "unauthorized", "target", "resolve"])
async def test_failure_before_membership_rpc_is_proven_not_executed(credentials, phase):
    client = AsyncMock()
    client.is_user_authorized.return_value = phase != "unauthorized"
    if phase == "authorization":
        client.is_user_authorized.side_effect = RuntimeError("authorization interrupted")
    if phase == "resolve":
        client.get_entity.side_effect = ValueError("entity missing")
    result = await invoke(
        client, credentials, target="" if phase == "target" else "channel",
        raw_session=None if phase == "session" else "test",
        connect_error=RuntimeError("connection interrupted") if phase == "connect" else None,
    )
    assert result.ok is False
    assert result.remote_mutation_started is False
    client.assert_not_awaited()


@pytest.mark.parametrize("target", ["channel", "https://t.me/+invitehash"])
async def test_response_lost_after_join_rpc_stays_unknown(credentials, target):
    client = AsyncMock(side_effect=RuntimeError("response lost"))
    client.is_user_authorized.return_value = True
    client.get_entity.return_value = MagicMock(id=1001)
    result = await invoke(client, credentials, target=target)
    assert result.ok is False
    assert result.remote_mutation_started is None
    client.assert_awaited_once()


@pytest.mark.parametrize("target", ["channel", "https://t.me/+invitehash"])
async def test_confirmed_membership_calls_one_rpc(credentials, target):
    client = AsyncMock()
    client.is_user_authorized.return_value = True
    client.get_entity.return_value = MagicMock(id=1001)
    result = await invoke(client, credentials, target=target)
    assert result.ok is True
    assert result.membership_status == "joined"
    assert result.remote_mutation_started is True
    client.assert_awaited_once()


async def test_already_participant_keeps_confirmed_membership(credentials):
    client = AsyncMock(side_effect=UserAlreadyParticipantError(request=None))
    client.is_user_authorized.return_value = True
    result = await invoke(client, credentials, target="https://t.me/+invitehash")
    assert result.ok is True
    assert result.membership_status == "already_joined"
    assert result.remote_mutation_started is False
    client.assert_awaited_once()
