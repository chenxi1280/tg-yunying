import pytest

from app.models import ChannelMessage, OperationTarget
from app.services.task_center.channel_payloads import ViewMessagePayload
from app.services.task_center.executors.common import channel_message_payload


pytestmark = pytest.mark.no_postgres


def test_channel_message_payload_freezes_channel_reference() -> None:
    channel = OperationTarget(
        id=18,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10018",
        username="channel_18",
        title="频道 18",
        reference_revision=3,
    )
    message = ChannelMessage(id=180, tenant_id=1, channel_target_id=18, message_id=181, content_preview="")

    payload = channel_message_payload(channel, message)

    assert payload["channel_target_id"] == 18
    assert payload["target_reference_revision"] == 3
    validated = ViewMessagePayload(**payload)
    assert validated.target_reference_revision == 3
    assert validated.target_reference_snapshot == {
        "tg_peer_id": "-10018",
        "username": "channel_18",
        "title": "频道 18",
    }
