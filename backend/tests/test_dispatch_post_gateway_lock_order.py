from types import SimpleNamespace

import pytest

from app.services.task_center import dispatcher


pytestmark = pytest.mark.no_postgres


def test_group_send_locks_dispatch_prefix_before_business_finalize(
    monkeypatch,
) -> None:
    events: list[str] = []
    action = SimpleNamespace(id="action-1", task_type="check_in")
    attempt = SimpleNamespace(id="attempt-1")
    payload = SimpleNamespace(
        message_text="hello",
        media_segments=[],
        reply_to_message_id=None,
    )
    context = SimpleNamespace(
        account=SimpleNamespace(id=11, session_ciphertext="cipher"),
        group=SimpleNamespace(id=7, tg_peer_id="-1007"),
        content="hello",
        credentials=object(),
        payload=payload,
    )
    result = SimpleNamespace(
        ok=True,
        remote_message_id="remote-1",
        failure_type="",
        detail="",
    )
    monkeypatch.setattr(
        dispatcher,
        "_reserve_group_send_attempt",
        lambda *_args: attempt,
    )
    monkeypatch.setattr(
        dispatcher.gateway,
        "send_message",
        lambda *_args, **_kwargs: events.append("gateway") or result,
    )
    monkeypatch.setattr(
        dispatcher,
        "_recover_send_message_required_channel",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        dispatcher,
        "_lock_post_gateway_dispatch_prefix",
        lambda *_args: events.append("dispatch_prefix"),
    )
    monkeypatch.setattr(
        dispatcher,
        "_finalize_group_send",
        lambda *_args, **_kwargs: events.append("business_finalize"),
    )

    assert dispatcher._send_group_message_via_gateway(object(), action, context)
    assert events == ["gateway", "dispatch_prefix", "business_finalize"]
