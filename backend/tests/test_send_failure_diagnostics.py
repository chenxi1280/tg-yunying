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


def test_permission_recovery_early_return_keeps_original_rpc_evidence(monkeypatch):
    from app.integrations.telegram.contracts import SendResult
    from app.services.task_center import dispatcher

    diagnostics = {"rpc_error_type": "ChatWriteForbiddenError", "failure_stage": "send_call",
        "send_call_started": True, "remote_mutation_state": "unknown"}
    result = SendResult(False, failure_type="群无权限", detail="cannot send", diagnostics=diagnostics)
    monkeypatch.setattr("app.services.task_center.group_mutation_authority.ensure_platform_writer_admission", lambda *a, **k: (True, ""))
    monkeypatch.setattr("app.services.task_center.ai_group_content_allocation.validate_content_intent_for_gateway", lambda *a, **k: None)
    monkeypatch.setattr(dispatcher, "_outbound_segments", lambda payload: None)
    monkeypatch.setattr(dispatcher.gateway, "send_message", lambda *a, **k: result)
    engine = _engine()
    with Session(engine) as session:
        action, attempt = _seed_started_attempt(session)
        bind_gateway_request_identity(action, attempt)
        account = session.get(TgAccount, action.account_id)
        context = SimpleNamespace(account=account, credentials=None, content="hello",
            group=SimpleNamespace(id=1, group_type="supergroup", tg_peer_id="-100123"),
            payload=SimpleNamespace(emergency_selection_id="", ai_generation_context_mode="",
                conversation_turn_claim_id="", reply_to_message_id=None, message_text="hello"))
        monkeypatch.setattr(dispatcher, "_reserve_group_send_attempt", lambda *a, **k: attempt)

        def recover(*args):
            dispatcher._fail(action, "群无权限", "cannot send", validation_stage="send_permission")
            dispatcher._finish_execution_attempt(attempt, action, failure_type="群无权限", detail="cannot send")
            return True

        monkeypatch.setattr(dispatcher, "_recover_send_message_required_channel", recover)
        assert dispatcher._send_group_message_via_gateway(session, action, context)
        session.flush()
        assert action.result["send_diagnostics"] == diagnostics
        assert attempt.result_snapshot["send_diagnostics"] == diagnostics
        assert action.status == "unknown_after_send"
        assert attempt.status == "result_unknown"
    engine.dispose()
