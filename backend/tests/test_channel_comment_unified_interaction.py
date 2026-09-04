from types import SimpleNamespace

import pytest

from app.services.task_center.executors import channel_comment


pytestmark = pytest.mark.no_postgres


def test_unified_default_comment_mode_prioritizes_real_human_replies(
    monkeypatch,
) -> None:
    task = SimpleNamespace()
    context = SimpleNamespace(
        config={"engagement_contract_version": "unified_engagement_v1"},
        channel=SimpleNamespace(id=31),
    )
    message = SimpleNamespace(id=41)
    monkeypatch.setattr(
        channel_comment,
        "_message_reply_targets",
        lambda *_args, **_kwargs: [
            {"message_id": 8101, "source": "channel_comment"},
            {"message_id": 8102, "source": "channel_comment"},
            {"message_id": 9101, "source": "own_history"},
        ],
    )

    targets = channel_comment._unified_comment_interaction_targets(
        object(), task, context=context, message=message, quantity=4,
    )

    assert [item and item["message_id"] for item in targets] == [8101, 8102, None, None]


def test_legacy_default_comment_mode_remains_top_level_only() -> None:
    task = SimpleNamespace()
    context = SimpleNamespace(config={}, channel=SimpleNamespace(id=31))

    targets = channel_comment._unified_comment_interaction_targets(
        object(), task, context=context, message=SimpleNamespace(id=41), quantity=4,
    )

    assert targets == [None, None, None, None]
