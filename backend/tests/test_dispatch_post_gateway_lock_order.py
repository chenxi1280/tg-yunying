from types import SimpleNamespace

import pytest

from app.services.task_center import dispatcher


pytestmark = pytest.mark.no_postgres


def test_group_send_locks_dispatch_prefix_before_business_finalize(
    monkeypatch,
) -> None:
    events: list[str] = []
    action = SimpleNamespace(
        id="action-1", tenant_id=1, task_id="task-1", task_type="check_in",
    )
    attempt = SimpleNamespace(
        id="attempt-1",
        result_snapshot={
            "telegram_gateway_timeout_seconds": 10,
            "telegram_connect_timeout_seconds": 5,
        },
    )
    payload = SimpleNamespace(
        message_text="hello",
        media_segments=[],
        reply_to_message_id=None,
        conversation_turn_claim_id="",
        group_id=7,
    )
    context = SimpleNamespace(
        account=SimpleNamespace(id=11, session_ciphertext="cipher"),
        group=SimpleNamespace(id=7, tg_peer_id="-1007", group_type="supergroup"),
        content="hello",
        credentials=object(),
        payload=payload,
        session_ciphertext="current-authorization-session",
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
        lambda *_args, **_kwargs: attempt,
    )
    monkeypatch.setattr(
        "app.services.task_center.ai_group_content_allocation.validate_content_intent_for_gateway",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.task_center.group_mutation_authority.ensure_platform_writer_admission",
        lambda *_args, **_kwargs: (True, ""),
    )
    def send_message(*args, **kwargs):
        assert args[4] == "current-authorization-session"
        assert kwargs["timeout_seconds"] == 10
        assert kwargs["connect_timeout_seconds"] == 5
        events.append("gateway")
        return result

    monkeypatch.setattr(dispatcher.gateway, "send_message", send_message)
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


def test_conversation_probe_runs_inside_reserved_bulkhead_before_call_issued(
    monkeypatch,
) -> None:
    events: list[str] = []
    action = SimpleNamespace(
        id="reply-action",
        tenant_id=1,
        task_id="task-1",
        task_type="group_ai_chat",
    )
    attempt = SimpleNamespace(id="attempt-1")
    payload = SimpleNamespace(
        message_text="reply",
        media_segments=[],
        reply_to_message_id=42,
        conversation_turn_claim_id="claim-1",
        group_id=7,
    )
    context = SimpleNamespace(
        account=SimpleNamespace(id=11, session_ciphertext="cipher"),
        group=SimpleNamespace(id=7, tg_peer_id="-1007", group_type="supergroup"),
        content="reply",
        credentials=object(),
        payload=payload,
    )

    def reserve(*_args, **kwargs):
        assert kwargs["start_gateway_call"] is False
        events.append("resources_reserved")
        return attempt

    monkeypatch.setattr(dispatcher, "_reserve_group_send_attempt", reserve)
    monkeypatch.setattr(
        "app.services.task_center.ai_group_content_allocation.validate_content_intent_for_gateway",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.task_center.group_mutation_authority.ensure_platform_writer_admission",
        lambda *_args, **_kwargs: (True, ""),
    )
    monkeypatch.setattr(
        dispatcher,
        "_conversation_remote_context_current",
        lambda *_args: events.append("remote_probe") or False,
    )
    monkeypatch.setattr(
        dispatcher,
        "_settle_group_send_preflight_attempt",
        lambda *_args: events.append("resources_released"),
    )
    monkeypatch.setattr(
        dispatcher.gateway,
        "send_message",
        lambda *_args, **_kwargs: events.append("mutation_call"),
    )

    assert dispatcher._send_group_message_via_gateway(object(), action, context)
    assert events == ["resources_reserved", "remote_probe", "resources_released"]
