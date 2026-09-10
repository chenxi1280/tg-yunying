import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from telethon import errors

from app.integrations.telegram import DeveloperAppCredentials, TelethonTelegramGateway
from app.models import GatewayRequestEvidenceJournal, TgAccount
from app.services.task_center.dispatcher import _apply_send_result
from app.services.task_center.gateway_evidence_journal import bind_gateway_request_identity
from test_gateway_evidence_journal import _engine, _seed_started_attempt

pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("phase,mutation", [("resolve_target", False), ("prepare_send", False), ("send_call", None)])
def test_permission_rpc_retains_phase_and_does_not_infer_unsent(monkeypatch, phase, mutation):
    gateway = TelethonTelegramGateway()
    error = errors.ChatWriteForbiddenError(request=SimpleNamespace(private_token="not serialized"))
    client = SimpleNamespace(is_user_authorized=AsyncMock(return_value=True),
        send_message=AsyncMock(side_effect=error))
    monkeypatch.setattr(gateway, "_get_or_create_client", AsyncMock(return_value=client))
    monkeypatch.setattr("app.integrations.telegram.gateway.resolve_telethon_target",
        AsyncMock(side_effect=error) if phase == "resolve_target" else AsyncMock(return_value=object()))
    monkeypatch.setattr("app.integrations.telegram.telethon_send.send_typing_action",
        AsyncMock(side_effect=error) if phase == "prepare_send" else AsyncMock())
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="test", credentials_version=1)
    result = asyncio.run(gateway._send_async("test-session", "neutral", "hello", None, credentials))
    assert result.remote_mutation_started is mutation
    assert result.diagnostics["rpc_error_type"] == "ChatWriteForbiddenError"
    assert result.diagnostics["failure_stage"] == phase
    assert result.diagnostics["send_call_started"] is (phase == "send_call")
    assert "private" not in repr(result.diagnostics)
    _assert_persisted_result(result)


def _assert_persisted_result(result):
    engine = _engine()
    with Session(engine) as session:
        action, attempt = _seed_started_attempt(session)
        account = session.get(TgAccount, action.account_id)
        bind_gateway_request_identity(action, attempt)
        _apply_send_result(action, account, False, failure_type=result.failure_type, detail=result.detail,
            attempt=attempt, remote_mutation_started=result.remote_mutation_started, send_diagnostics=result.diagnostics)
        session.flush()
        assert action.result["send_diagnostics"] == result.diagnostics
        assert attempt.result_snapshot["send_diagnostics"] == result.diagnostics
        journal = session.scalar(select(GatewayRequestEvidenceJournal).where(
            GatewayRequestEvidenceJournal.execution_attempt_id == attempt.id))
        assert journal.remote_mutation_state == result.diagnostics["remote_mutation_state"]
        if result.remote_mutation_started is None:
            assert action.status == "unknown_after_send"
            assert attempt.status == "result_unknown"
    engine.dispose()
