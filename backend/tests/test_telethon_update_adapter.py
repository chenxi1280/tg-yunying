from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from telethon import types

pytestmark = pytest.mark.no_postgres

from app.integrations.telegram.telethon_updates import fetch_update_difference


class _FakeClient:
    def __init__(self, response) -> None:
        self.response = response

    async def __call__(self, _request):
        return self.response


def test_common_difference_normalizes_lifecycle_and_outbound_mapping() -> None:
    peer = types.PeerChannel(123)
    message = types.Message(
        id=11,
        peer_id=peer,
        date=datetime.now(timezone.utc),
        message="hello",
        from_id=types.PeerUser(99),
        entities=[types.MessageEntityBold(offset=0, length=5)],
    )
    topic = types.MessageService(
        id=12,
        peer_id=peer,
        date=datetime.now(timezone.utc),
        action=types.MessageActionTopicCreate(title="Topic", icon_color=1),
    )
    updates = [
        types.UpdateNewChannelMessage(message=message, pts=101, pts_count=1),
        types.UpdateNewChannelMessage(message=topic, pts=102, pts_count=1),
        types.UpdateDeleteChannelMessages(channel_id=123, messages=[10], pts=103, pts_count=1),
        types.UpdatePinnedChannelMessages(channel_id=123, messages=[11], pts=104, pts_count=1, pinned=True),
        types.UpdateMessageID(id=9001, random_id=777),
    ]
    response = types.updates.Difference(
        new_messages=[],
        new_encrypted_messages=[],
        other_updates=updates,
        chats=[],
        users=[],
        state=types.updates.State(
            pts=20,
            qts=0,
            date=datetime.now(timezone.utc),
            seq=3,
            unread_count=0,
        ),
    )

    batch = asyncio.run(fetch_update_difference(
        _FakeClient(response),
        pts=19,
        qts=0,
        date=1,
    ))

    assert batch.status == "live" and batch.cursor["pts"] == 20
    items = [item for envelope in batch.updates for item in envelope.normalized_items]
    assert [item["event_type"] for item in items] == [
        "message_new", "topic_create", "message_delete", "message_pin",
    ]
    assert items[0]["entities"] == [{"type": "bold", "offset": 0, "length": 5}]
    assert all(envelope.routing_peer_id in {None, "-1000000000123"} for envelope in batch.updates)
    assert batch.outbound_mappings[0].random_id == 777
    assert batch.outbound_mappings[0].remote_message_id == 9001


def test_difference_too_long_stays_explicitly_non_final() -> None:
    response = types.updates.DifferenceTooLong(pts=999)
    batch = asyncio.run(fetch_update_difference(
        _FakeClient(response),
        pts=10,
        qts=0,
        date=1,
    ))
    assert batch.status == "too_long"
    assert batch.final is False
    assert batch.cursor == {"pts": 999}
