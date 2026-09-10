import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.integrations.telegram import telethon_content
from app.integrations.telegram.message_observation import GroupMessageObservation
from test_task_group_bot_post_send_recovery import _session, _seed, _prompt, _recover

pytestmark = pytest.mark.no_postgres
OBSERVED_AT = datetime(2026, 9, 10, tzinfo=timezone.utc)


def _raw_message(message_id, *, controls=False):
    return SimpleNamespace(id=message_id, message="private message body", date=OBSERVED_AT,
        buttons=[[SimpleNamespace(text="verify", data=b"private-callback")]] if controls else [],
        get_sender=AsyncMock(return_value=SimpleNamespace(id=11, bot=True, first_name="bot")))


@pytest.mark.parametrize("raw_count,controls", [(0, False), (2, False), (2, True)])
def test_bounded_read_preserves_raw_and_filtered_counts(monkeypatch, raw_count, controls):
    raw = [_raw_message(index + 601, controls=controls) for index in range(raw_count)]
    observed = {}
    async def iter_messages(target, **kwargs):
        observed.update(kwargs)
        for row in raw:
            yield row
    client = SimpleNamespace(iter_messages=iter_messages,
        get_me=AsyncMock(return_value=SimpleNamespace(id=99)),
        get_permissions=AsyncMock(side_effect=TimeoutError("private RPC parameters")))
    monkeypatch.setattr(telethon_content, "resolve_telethon_target", AsyncMock(return_value=object()))
    result = asyncio.run(telethon_content.fetch_group_messages(client, "neutral", 2,
        control_only=True, after_message_id=600, include_diagnostics=True))
    assert result.diagnostics["raw_message_count"] == raw_count
    assert result.diagnostics["candidate_message_count"] == (raw_count if controls else 0)
    assert result.diagnostics["excluded_no_controls_count"] == (0 if controls else raw_count)
    assert result.diagnostics["limit_reached"] is (raw_count == 2)
    assert observed == {"min_id": 600, "reverse": True, "limit": 2}
    if controls:
        assert all(row.sender_role == "unknown" and row.sender_role_error == "TimeoutError" for row in result.messages)
        assert client.get_permissions.await_count == 1
    assert "private" not in repr(result.diagnostics)


@pytest.mark.parametrize("raw_count,reason", [(0, "empty_window"), (3, "no_buttons")])
def test_empty_window_and_filtered_text_are_distinct_persisted_reasons(raw_count, reason):
    with _session() as session:
        action, admission = _seed(session)
        observation = GroupMessageObservation((), {"read_status": "observed", "raw_message_count": raw_count,
            "candidate_message_count": 0, "excluded_no_controls_count": raw_count})
        result = _recover(session, action, lambda *args, **kwargs: observation)
        assert result.reason == f"post_send_control_{reason}"
        assert action.result["post_send_control_diagnostics"]["raw_message_count"] == raw_count
        assert admission.terminal_evidence["control_observation"] == result.diagnostics


def test_role_lookup_failure_is_recorded_without_trusting_the_source():
    with _session() as session:
        action, admission = _seed(session)
        message = _prompt(sender_role="unknown")
        message.sender_role_error = "ChatAdminRequiredError"
        result = _recover(session, action, lambda *args, **kwargs: [message])
        assert result.reason == "post_send_control_source_role_lookup_failed"
        assert result.diagnostics["source_role_error_counts"] == {"ChatAdminRequiredError": 1}
        assert admission.state == "post_send_intercepted"


def test_fetch_error_does_not_become_successful_zero_message_read():
    with _session() as session:
        action, admission = _seed(session)
        def failed_fetch(*args, **kwargs):
            raise TimeoutError("private transport details")
        result = _recover(session, action, failed_fetch)
        evidence = action.result["post_send_control_diagnostics"]
        assert result.status == "retry" and admission.state == "ready"
        assert evidence["read_status"] == "failed"
        assert evidence["error_type"] == "TimeoutError"
        assert "raw_message_count" not in evidence and "private" not in repr(evidence)
