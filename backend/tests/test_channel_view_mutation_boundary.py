from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations.telegram.contracts import DeveloperAppCredentials
from app.integrations.telegram.gateway import TelethonTelegramGateway


pytestmark = [pytest.mark.no_postgres, pytest.mark.anyio]


@pytest.fixture
def credentials():
    return DeveloperAppCredentials(
        app_id=1, api_id=12345, api_hash="test", credentials_version=1,
        app_name="test",
    )


async def invoke_view(gateway, client, credentials):
    with patch.object(gateway, "_get_or_create_client", return_value=client), patch(
        "app.integrations.telegram.gateway.decrypt_session", return_value="test"
    ):
        return await gateway._view_channel_message_async(
            "encrypted", "test-channel", 501, credentials,
        )


@pytest.mark.parametrize("error", [
    ValueError('No user has "missing-channel" as username'),
    RuntimeError("entity lookup interrupted"),
])
async def test_resolution_failure_proves_view_rpc_not_called(credentials, error):
    client = AsyncMock()
    client.is_user_authorized.return_value = True
    client.get_entity.side_effect = error
    result = await invoke_view(TelethonTelegramGateway(), client, credentials)
    assert result.ok is False
    assert result.remote_mutation_started is False
    client.assert_not_called()


async def test_unknown_after_view_rpc_stays_unknown(credentials):
    client = AsyncMock(side_effect=RuntimeError("response lost"))
    client.is_user_authorized.return_value = True
    client.get_entity.return_value = MagicMock(id=1001)
    result = await invoke_view(TelethonTelegramGateway(), client, credentials)
    assert result.ok is False
    assert result.remote_mutation_started is None
    client.assert_awaited_once()


async def test_confirmed_view_has_exactly_one_mutation_rpc(credentials):
    client = AsyncMock()
    client.is_user_authorized.return_value = True
    client.get_entity.return_value = MagicMock(id=1001)
    result = await invoke_view(TelethonTelegramGateway(), client, credentials)
    assert result.ok is True
    assert result.remote_mutation_started is True
    client.assert_awaited_once()
    assert client.call_args.args[0].increment is True
    assert client.call_args.args[0].id == [501]
