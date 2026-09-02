from types import SimpleNamespace

import pytest

from app.integrations.telegram import SendResult
from app.models import TaskStatus
from app.services import operations


pytestmark = pytest.mark.no_postgres


def _reply_scope() -> operations.OperationExecutionScope:
    return operations.OperationExecutionScope(
        session=object(),
        task=SimpleNamespace(task_type="CHANNEL_REPLY", reaction=""),
        attempt=SimpleNamespace(content="真实评论", reaction=""),
        target=None,
        channel_message=SimpleNamespace(message_id=91),
        channel=SimpleNamespace(tg_peer_id="-10031"),
        account=SimpleNamespace(
            id=7, session_ciphertext="ciphertext", last_active_at=None,
        ),
        credentials=object(),
    )


def test_channel_reply_gateway_uses_keyword_contract(monkeypatch) -> None:
    scope = _reply_scope()
    observed = {}
    monkeypatch.setattr(
        operations, "_reserve_operation_gateway_attempt",
        lambda *_args: (scope.channel, scope.attempt, "", ""),
    )

    def send(account_id, peer_id, **kwargs):
        observed.update(account_id=account_id, peer_id=peer_id, **kwargs)
        return SendResult(True, remote_message_id="remote-91")

    monkeypatch.setattr(operations.gateway, "reply_channel_message", send)

    outcome = operations._operation_gateway_call(scope)

    assert outcome == operations.OperationGatewayOutcome(
        True, "", "", "remote-91",
    )
    assert observed["message_id"] == 91
    assert observed["content"] == "真实评论"
    assert observed["session_ciphertext"] == "ciphertext"


def test_channel_reply_reservation_rejection_stops_before_gateway(monkeypatch) -> None:
    scope = _reply_scope()
    monkeypatch.setattr(
        operations, "_reserve_operation_gateway_attempt",
        lambda *_args: (
            scope.channel, scope.attempt, "target_ref_invalid", "引用已失效",
        ),
    )
    monkeypatch.setattr(
        operations.gateway, "reply_channel_message",
        lambda *_args, **_kwargs: pytest.fail("reservation reject must stop gateway"),
    )

    outcome = operations._operation_gateway_call(scope)

    assert outcome.ok is None
    assert outcome.failure_type == "target_ref_invalid"


def test_success_outcome_settles_attempt_and_releases_authority(monkeypatch) -> None:
    scope = _reply_scope()
    scope.attempt.status = "executing"
    scope.attempt.failure_type = "old"
    scope.attempt.failure_detail = "old"
    scope.attempt.remote_message_id = ""
    scope.attempt.executed_at = None
    released = []
    monkeypatch.setattr(
        operations, "_release_operation_task_authority",
        lambda _session, _task, target: released.append(target),
    )

    result = operations._settle_operation_outcome(
        scope.session,
        scope.task,
        attempt=scope.attempt,
        account=scope.account,
        target=scope.channel,
        outcome=operations.OperationGatewayOutcome(True, remote_id="remote-91"),
    )

    assert result == (True, "", "")
    assert scope.attempt.status == TaskStatus.COMPLETED.value
    assert scope.attempt.remote_message_id == "remote-91"
    assert scope.attempt.executed_at is not None
    assert released == [scope.channel]
