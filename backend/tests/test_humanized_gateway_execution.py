from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.integrations.telegram.contracts import DeveloperAppCredentials
from app.integrations.telegram.gateway import TelethonTelegramGateway

pytestmark = pytest.mark.no_postgres
BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_manual_channel_action_script_is_preview_only_by_default():
    source = (BACKEND_ROOT / "scripts/test_humanized_channel_actions.py").read_text()

    assert 'parser.add_argument("--apply", action="store_true")' in source
    assert '"mutations_executed": False' in source
    assert "asyncio.sleep" not in source
    assert "GetHistoryRequest" not in source
    assert "SetTypingRequest" not in source


def test_channel_capability_probe_uses_production_parser():
    source = (BACKEND_ROOT / "scripts/probe_channel_interactive_capabilities.py").read_text()

    assert "resolve_channel_reaction_capability" in source
    assert '"reactions_mode": capability.mode' in source


@pytest.fixture
def mock_credentials():
    return DeveloperAppCredentials(
        app_id=1,
        api_id=12345,
        api_hash="mock_api_hash_12345",
        credentials_version=1,
        app_name="testapp",
    )


@pytest.mark.anyio
async def test_view_channel_message_has_no_pre_mutation_delay_or_auxiliary_rpc(mock_credentials):
    gateway = TelethonTelegramGateway()
    mock_client = AsyncMock()
    mock_client.is_user_authorized.return_value = True
    mock_client.get_entity.return_value = MagicMock(id=1001)

    with patch.object(gateway, "_get_or_create_client", return_value=mock_client), \
         patch("app.integrations.telegram.gateway.decrypt_session", return_value="1ApW..."), \
         patch("asyncio.sleep", new_callable=AsyncMock, side_effect=AssertionError("unexpected sleep")):

        result = await gateway._view_channel_message_async(
            session_ciphertext="enc_session",
            channel_peer_id="-1001001",
            message_id=501,
            credentials=mock_credentials,
        )

        assert result.ok is True
        assert "message_id=501" in result.detail
        assert mock_client.call_count == 1


@pytest.mark.anyio
async def test_send_reaction_has_no_pre_mutation_delay(mock_credentials):
    gateway = TelethonTelegramGateway()
    mock_client = AsyncMock()
    mock_client.is_user_authorized.return_value = True
    mock_client.get_entity.return_value = MagicMock(id=1001)

    with patch.object(gateway, "_get_or_create_client", return_value=mock_client), \
         patch("app.integrations.telegram.gateway.decrypt_session", return_value="1ApW..."), \
         patch("asyncio.sleep", new_callable=AsyncMock, side_effect=AssertionError("unexpected sleep")):

        result = await gateway._send_channel_reaction_async(
            session_ciphertext="enc_session",
            channel_peer_id="-1001001",
            message_id=501,
            reaction="🔥",
            credentials=mock_credentials,
        )

        assert result.ok is True
        assert "reaction=🔥" in result.detail
        assert mock_client.call_count == 1


@pytest.mark.anyio
async def test_reply_channel_message_has_no_typing_rpc_or_pre_mutation_delay(mock_credentials):
    gateway = TelethonTelegramGateway()
    mock_client = AsyncMock()
    mock_client.is_user_authorized.return_value = True
    mock_client.get_entity.return_value = MagicMock(id=1001)
    mock_sent_msg = MagicMock(id=9901)
    mock_client.send_message.return_value = mock_sent_msg

    with patch.object(gateway, "_get_or_create_client", return_value=mock_client), \
         patch("app.integrations.telegram.gateway.decrypt_session", return_value="1ApW..."), \
         patch("asyncio.sleep", new_callable=AsyncMock, side_effect=AssertionError("unexpected sleep")):

        result = await gateway._reply_channel_message_async(
            session_ciphertext="enc_session",
            channel_peer_id="-1001001",
            message_id=501,
            content="看着挺润的啊老哥",
            credentials=mock_credentials,
            reply_to_message_id=None,
        )

        assert result.ok is True
        assert result.remote_message_id == "9901"
        assert mock_client.call_count == 0
        mock_client.send_message.assert_called_once()
