from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.integrations.telegram.contracts import OperationResult
from app.services.task_center import dispatcher
from app.services.task_center.payloads import DeleteMessagePayload


pytestmark = pytest.mark.no_postgres


def test_delete_message_keeps_generic_attempt_path(monkeypatch) -> None:
    calls: list[str] = []
    session = MagicMock()
    session.get.return_value = None
    action = SimpleNamespace(tenant_id=1)
    account = SimpleNamespace(id=7, session_ciphertext="session")
    attempt = SimpleNamespace(id="attempt-delete")
    monkeypatch.setattr(
        dispatcher, "_platform_mutation_admitted", lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        dispatcher, "_begin_execution_attempt",
        lambda *_args, **_kwargs: calls.append("begin") or attempt,
    )
    monkeypatch.setattr(dispatcher, "_mark_executing", lambda *_args, **_kwargs: calls.append("executing"))
    monkeypatch.setattr(
        dispatcher, "_mark_gateway_call_started",
        lambda *_args, **_kwargs: calls.append("gateway_started"),
    )
    monkeypatch.setattr(
        dispatcher.gateway, "delete_message",
        lambda *_args, **_kwargs: calls.append("delete") or OperationResult(True),
    )
    monkeypatch.setattr(
        dispatcher, "_apply_operation_result",
        lambda *_args, **_kwargs: calls.append("applied"),
    )

    result = dispatcher._dispatch_delete_message(
        session, action, account, None,
        DeleteMessagePayload(chat_id="-100123", message_id="77"),
    )

    assert result is True
    assert calls == ["begin", "executing", "gateway_started", "delete", "applied"]
    session.commit.assert_called_once_with()
