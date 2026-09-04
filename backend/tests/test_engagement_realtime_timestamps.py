from datetime import datetime, timezone

import pytest

from app.services.task_center import channel_comment_realtime
from app.services.task_center.channel_comment_update_stream import _sent_at as comment_sent_at
from app.services.task_center.engagement_conversation import _naive as conversation_time
from app.services.task_center.engagement_conversation_wake import _naive as wake_time
from app.services.task_center.group_ai_update_stream import _sent_at as group_sent_at


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("value", ["2026-09-04T04:00:00+00:00", "2026-09-04T12:00:00+08:00", "2026-09-04T12:00:00"])
def test_group_and_comment_stream_share_beijing_clock(value) -> None:
    expected = datetime(2026, 9, 4, 12)
    assert group_sent_at(value, expected) == expected
    assert comment_sent_at(value) == expected
    assert conversation_time(datetime.fromisoformat(value)) == expected
    assert wake_time(datetime.fromisoformat(value)) == expected


def test_group_observation_timestamp_is_normalized_when_message_date_absent() -> None:
    assert group_sent_at(None, datetime(2026, 9, 4, 4, tzinfo=timezone.utc)) == datetime(2026, 9, 4, 12)


def test_comment_source_deadline_compares_instants_not_offset_wall_clocks(monkeypatch) -> None:
    monkeypatch.setattr(channel_comment_realtime, "comment_source_window", lambda *_: (
        datetime(2026, 9, 4, 8), datetime(2026, 9, 4, 12),
    ))
    assert channel_comment_realtime._inside_source_window(
        object(), object(), datetime(2026, 9, 4, 3, 59, tzinfo=timezone.utc),
    )
    assert not channel_comment_realtime._inside_source_window(
        object(), object(), datetime(2026, 9, 4, 4, 1, tzinfo=timezone.utc),
    )
