from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from telethon import errors as telethon_errors

from app.database import Base
from app.integrations.telegram import DeveloperAppCredentials, TelethonTelegramGateway
from app.integrations.telegram.contracts import OutboundSegment
from app.models import (
    Action,
    AuditLog,
    ExecutionAttempt,
    FailureType,
    GatewayRequestEvidenceJournal,
    Task,
    Tenant,
)
from app.schemas.task_center import TaskRetryRequest
from app.services.task_center.service import retry_task


# ---------------------------------------------------------------------------
# RC-10a：gateway _send_async 异常边界
# ---------------------------------------------------------------------------


def _credentials() -> DeveloperAppCredentials:
    return DeveloperAppCredentials(
        app_id=1,
        api_id=123,
        api_hash="hash",
        credentials_version=1,
        app_name="pytest",
    )


class _FakeClient:
    async def is_user_authorized(self) -> bool:
        return True


async def _skip_typing(*_args, **_kwargs) -> None:
    return None


@pytest.mark.no_postgres
def test_send_async_maps_generic_error_when_target_resolve_fails(monkeypatch) -> None:
    """target resolve 抛错必须返回映射后的 SendResult，而不是 UnboundLocalError。"""
    gateway = TelethonTelegramGateway()

    async def fake_get_client(credentials, raw_session):  # noqa: ANN001 - mirrors gateway hook.
        return _FakeClient()

    async def fake_resolve(client, peer_id, *, group_id=0):  # noqa: ANN001
        raise RuntimeError("entity resolve exploded")

    monkeypatch.setattr(gateway, "_get_or_create_client", fake_get_client)
    monkeypatch.setattr(
        "app.integrations.telegram.gateway.resolve_telethon_target", fake_resolve
    )

    result = asyncio.run(
        gateway._send_async("raw-session", "@peer", "hello", None, _credentials())
    )

    assert result.ok is False
    assert result.failure_type == FailureType.UNKNOWN.value
    assert result.remote_mutation_started is False
    assert not (result.remote_message_id or "")


@pytest.mark.no_postgres
def test_send_async_maps_peer_invalid_when_target_resolve_fails(monkeypatch) -> None:
    gateway = TelethonTelegramGateway()

    async def fake_get_client(credentials, raw_session):  # noqa: ANN001
        return _FakeClient()

    async def fake_resolve(client, peer_id, *, group_id=0):  # noqa: ANN001
        raise telethon_errors.PeerIdInvalidError(request=None)

    monkeypatch.setattr(gateway, "_get_or_create_client", fake_get_client)
    monkeypatch.setattr(
        "app.integrations.telegram.gateway.resolve_telethon_target", fake_resolve
    )

    result = asyncio.run(
        gateway._send_async("raw-session", "@peer", "hello", None, _credentials())
    )

    assert result.ok is False
    assert result.failure_type == FailureType.PEER_INVALID.value
    assert result.remote_mutation_started is False


@pytest.mark.no_postgres
def test_send_async_keeps_unknown_when_first_send_response_is_lost(monkeypatch) -> None:
    gateway = TelethonTelegramGateway()

    class _ResponseLostClient(_FakeClient):
        async def send_message(  # noqa: ANN001
            self, target, content, reply_to=None, link_preview=False
        ):
            raise TimeoutError("response lost after send started")

    async def fake_get_client(credentials, raw_session):  # noqa: ANN001
        return _ResponseLostClient()

    async def fake_resolve(client, peer_id, *, group_id=0):  # noqa: ANN001
        return object()

    monkeypatch.setattr(gateway, "_get_or_create_client", fake_get_client)
    monkeypatch.setattr(
        "app.integrations.telegram.gateway.resolve_telethon_target", fake_resolve
    )
    monkeypatch.setattr(
        "app.integrations.telegram.telethon_send.send_typing_action", _skip_typing
    )

    result = asyncio.run(
        gateway._send_async("raw-session", "@peer", "hello", None, _credentials())
    )

    assert result.ok is False
    assert result.remote_message_id is None
    assert result.remote_mutation_started is None


@pytest.mark.no_postgres
def test_send_async_preserves_remote_identity_on_partial_segment_failure(monkeypatch) -> None:
    """首个 segment 已送达后第二个失败：保留远端 identity，禁止整体自动重发。"""
    gateway = TelethonTelegramGateway()

    class _SegClient(_FakeClient):
        def __init__(self) -> None:
            self.calls = 0

        async def send_message(  # noqa: ANN001
            self, target, content, reply_to=None, link_preview=False
        ):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("second segment send exploded")
            return type("Msg", (), {"id": 111})()

    client = _SegClient()

    async def fake_get_client(credentials, raw_session):  # noqa: ANN001
        return client

    async def fake_resolve(client_arg, peer_id, *, group_id=0):  # noqa: ANN001
        return object()

    monkeypatch.setattr(gateway, "_get_or_create_client", fake_get_client)
    monkeypatch.setattr(
        "app.integrations.telegram.gateway.resolve_telethon_target", fake_resolve
    )
    monkeypatch.setattr(
        "app.integrations.telegram.telethon_send.send_typing_action", _skip_typing
    )

    segments = [
        OutboundSegment(segment_type="文本", content="first"),
        OutboundSegment(segment_type="文本", content="second"),
    ]

    result = asyncio.run(
        gateway._send_async(
            "raw-session", "@peer", "", segments, _credentials()
        )
    )

    assert result.ok is False
    assert result.remote_message_id == "111"
    assert result.remote_mutation_started is True


# ---------------------------------------------------------------------------
# RC-10b：generic retry 安全闭集
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=1, name="默认运营空间"))
        db.commit()
        yield db


def _task() -> Task:
    return Task(tenant_id=1, name="浏览", type="channel_view", status="running", stats={})


def _action(task: Task, status: str, result: dict | None = None) -> Action:
    return Action(
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="view_message",
        status=status,
        payload={},
        result=dict(result or {}),
    )


def _attempt(
    action: Action,
    *,
    gateway_started: bool = False,
    remote_id: str = "",
    attempt_no: int = 1,
) -> ExecutionAttempt:
    return ExecutionAttempt(
        tenant_id=1,
        action_id=action.id,
        attempt_no=attempt_no,
        status="failed",
        gateway_call_started_at=datetime(2026, 8, 15, 10, 0) if gateway_started else None,
        remote_message_id=remote_id,
        result_snapshot={},
    )


def _journal(action: Action, attempt: ExecutionAttempt, state: str) -> GatewayRequestEvidenceJournal:
    return GatewayRequestEvidenceJournal(
        tenant_id=1,
        action_id=action.id,
        execution_attempt_id=attempt.id,
        gateway_request_identity=f"telegram-gateway:{attempt.id}",
        request_fingerprint="r" * 64,
        target_fingerprint="t" * 64,
        result_fingerprint="x" * 64,
        evidence_hash="e" * 64,
        remote_mutation_state=state,
    )


@pytest.mark.no_postgres
def test_retry_failed_only_false_never_reopens_success_pending_executing_claiming_or_unknown(session: Session) -> None:
    task = _task()
    session.add(task)
    session.flush()
    guarded: dict[str, Action] = {}
    for status in ("success", "pending", "executing", "claiming", "unknown_after_send"):
        action = _action(task, status)
        if status == "success":
            action.result = {"success": True, "telegram_msg_id": "abc"}
        session.add(action)
        guarded[status] = action
    session.commit()

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=False), "tester")

    for status, action in guarded.items():
        session.refresh(action)
        assert action.status == status
    session.refresh(guarded["success"])
    assert guarded["success"].result == {"success": True, "telegram_msg_id": "abc"}


@pytest.mark.no_postgres
def test_retry_rejects_gateway_started_failure_with_typed_blocker(session: Session) -> None:
    task = _task()
    session.add(task)
    session.flush()
    action = _action(task, "failed", {"success": False, "error_code": "send_error"})
    session.add(action)
    session.flush()
    session.add(_attempt(action, gateway_started=True))
    session.commit()

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=False), "tester")

    session.refresh(action)
    assert action.status == "failed"
    assert action.result["retry_skipped_reason"] == "unsafe_retry_gateway_outcome_unknown"


@pytest.mark.no_postgres
def test_retry_rejects_gateway_started_failure_even_with_journal_false(session: Session) -> None:
    task = _task()
    session.add(task)
    session.flush()
    action = _action(task, "failed", {"success": False, "error_code": "send_error"})
    session.add(action)
    session.flush()
    attempt = _attempt(action, gateway_started=True)
    session.add(attempt)
    session.flush()
    session.add(_journal(action, attempt, "false"))
    session.commit()

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=False), "tester")

    session.refresh(action)
    assert action.status == "failed"
    assert action.result["retry_skipped_reason"] == "unsafe_retry_gateway_outcome_unknown"


@pytest.mark.no_postgres
def test_retry_rejects_gateway_started_failure_even_with_typed_false_result(session: Session) -> None:
    task = _task()
    session.add(task)
    session.flush()
    action = _action(
        task,
        "failed",
        {"success": False, "error_code": "send_error", "remote_mutation_started": False},
    )
    session.add(action)
    session.flush()
    session.add(_attempt(action, gateway_started=True))
    session.commit()

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=False), "tester")

    session.refresh(action)
    assert action.status == "failed"
    assert action.result["retry_skipped_reason"] == "unsafe_retry_gateway_outcome_unknown"


@pytest.mark.no_postgres
def test_retry_rejects_failure_with_remote_identity_present(session: Session) -> None:
    task = _task()
    session.add(task)
    session.flush()
    action = _action(task, "failed", {"success": False, "error_code": "send_error"})
    session.add(action)
    session.flush()
    session.add(_attempt(action, gateway_started=True, remote_id="555"))
    session.commit()

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=False), "tester")

    session.refresh(action)
    assert action.status == "failed"
    assert action.result["retry_skipped_reason"] == "unsafe_retry_remote_identity_present"


@pytest.mark.no_postgres
def test_retry_allows_pre_gateway_failure_without_gateway_start(session: Session) -> None:
    task = _task()
    session.add(task)
    session.flush()
    action = _action(
        task,
        "failed",
        {
            "success": False,
            "error_code": "content_rejected",
            "remote_mutation_started": False,
        },
    )
    session.add(action)
    session.flush()
    session.add(_attempt(action, gateway_started=False))
    session.commit()

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=True), "tester")

    session.refresh(action)
    assert action.status == "pending"


@pytest.mark.no_postgres
def test_retry_allows_failed_action_without_attempt(session: Session) -> None:
    task = _task()
    session.add(task)
    session.flush()
    action = _action(
        task,
        "failed",
        {"success": False, "error_code": "x", "remote_mutation_started": False},
    )
    session.add(action)
    session.commit()

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=True), "tester")

    session.refresh(action)
    assert action.status == "pending"


@pytest.mark.no_postgres
def test_retry_rejects_failed_action_without_typed_remote_evidence(session: Session) -> None:
    task = _task()
    session.add(task)
    session.flush()
    action = _action(task, "failed", {"success": False, "error_code": "x"})
    session.add(action)
    session.commit()

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=False), "tester")

    session.refresh(action)
    assert action.status == "failed"
    assert action.result["retry_skipped_reason"] == "unsafe_retry_evidence_missing"


@pytest.mark.no_postgres
def test_retry_without_safe_actions_keeps_task_lifecycle(session: Session) -> None:
    task = _task()
    task.status = "failed"
    task.last_error = "original"
    session.add(task)
    session.flush()
    session.add(_action(task, "success", {"success": True, "telegram_msg_id": "abc"}))
    session.commit()

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=False), "tester")

    session.refresh(task)
    assert task.status == "failed"
    assert task.last_error == "original"


@pytest.mark.no_postgres
def test_retry_failed_only_controls_selection_breadth_within_safe_closure(session: Session) -> None:
    """failed_only 只扩大安全闭集内的选择范围（skipped/cancelled），不能触碰 unsafe 状态。"""
    task = _task()
    session.add(task)
    session.flush()
    safe_result = {
        "success": False,
        "error_code": "x",
        "remote_mutation_started": False,
    }
    skipped = _action(task, "skipped", safe_result)
    cancelled = _action(task, "cancelled", safe_result)
    session.add_all([skipped, cancelled])
    session.commit()

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=True), "tester")
    session.refresh(skipped)
    session.refresh(cancelled)
    assert skipped.status == "skipped"
    assert cancelled.status == "cancelled"

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=False), "tester")
    session.refresh(skipped)
    session.refresh(cancelled)
    assert skipped.status == "pending"
    assert cancelled.status == "pending"


@pytest.mark.no_postgres
def test_retry_rejects_unknown_after_send_with_generic_blocker(session: Session) -> None:
    task = _task()
    session.add(task)
    session.flush()
    action = _action(task, "unknown_after_send", {"success": False, "error_code": "unknown_after_send"})
    session.add(action)
    session.commit()

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=True), "tester")

    session.refresh(action)
    assert action.status == "unknown_after_send"
    assert action.result["retry_skipped_reason"] == "unsafe_retry_gateway_outcome_unknown"

    retry_task(session, 1, task.id, TaskRetryRequest(failed_only=True), "tester")

    audits = list(session.scalars(select(AuditLog).order_by(AuditLog.id)))
    assert len(audits) == 2
    assert all("retried=0 rejected=1" in item.detail for item in audits)
