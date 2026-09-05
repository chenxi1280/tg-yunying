from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, types

from app.integrations.telegram.telethon_send import SendProgress, send_content


pytestmark = [pytest.mark.no_postgres, pytest.mark.anyio]


@pytest.mark.parametrize("target", [types.User(id=42, is_self=True), types.InputPeerSelf()])
async def test_saved_messages_sends_without_unsupported_typing(target):
    client = AsyncMock()
    client.side_effect = errors.PeerIdInvalidError(request=None)
    client.send_message.return_value = SimpleNamespace(id=71)
    progress = SendProgress()

    await send_content(
        client, target, content="ABC E4", segments=None,
        reply_to_message_id=None, progress=progress,
    )

    client.assert_not_awaited()
    client.send_message.assert_awaited_once_with(
        target, "ABC E4", reply_to=None, link_preview=False,
    )
    assert progress.send_call_started is True
    assert progress.remote_message_id == "71"


async def test_other_user_typing_failure_does_not_start_message_send():
    client = AsyncMock()
    client.side_effect = errors.PeerIdInvalidError(request=None)
    progress = SendProgress()

    with pytest.raises(errors.PeerIdInvalidError):
        await send_content(
            client, types.User(id=43, is_self=False), content="hello", segments=None,
            reply_to_message_id=None, progress=progress,
        )

    client.send_message.assert_not_awaited()
    assert progress.send_call_started is False
    assert progress.remote_message_id is None


async def test_saved_messages_response_lost_preserves_started_boundary():
    client = AsyncMock()
    client.send_message.side_effect = TimeoutError("response lost")
    progress = SendProgress()

    with pytest.raises(TimeoutError):
        await send_content(
            client, types.InputPeerSelf(), content="ABC E4", segments=None,
            reply_to_message_id=None, progress=progress,
        )

    client.assert_not_awaited()
    assert progress.send_call_started is True
    assert progress.remote_message_id is None
