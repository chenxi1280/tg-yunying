from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker, Session

from app.models import ExecutionAttempt, AccountPoolConcurrencyLease, AccountBehaviorBudgetReservation
from app.services._common import _now
from app.services.task_center import dispatcher, engagement_runtime_resources as runtime
from app.telethon_lifecycle import TelethonOperationTimeout
from app.services.task_center.telegram_termination import (
    PendingTerminations, TerminationReceipt, register_termination, drain_telegram_terminations,
)
from test_engagement_runtime_resources import _session, _seed, _attempt

pytestmark = pytest.mark.no_postgres


def _unknown(session):
    task = _seed(session)
    action, attempt = _attempt(session, task, 11)
    runtime.reserve_attempt_resources(session, action, attempt)
    runtime.mark_attempt_call_issued(session, attempt)
    attempt.gateway_call_started_at = _now()
    attempt.status, action.status = "result_unknown", "unknown_after_send"
    attempt.result_snapshot = {**attempt.result_snapshot, "transport_termination_state": "cancellation_unconfirmed"}
    runtime.settle_attempt_resources(attempt, action, remote_mutation_started=None)
    session.commit()
    return action, attempt


def test_late_ack_persists_then_releases_only_physical_capacity():
    registry, terminated = PendingTerminations(), Event()
    with _session() as session:
        action, attempt = _unknown(session)
        factory = sessionmaker(session.get_bind())
        register_termination(attempt, terminated, registry=registry)
        assert drain_telegram_terminations(factory, registry=registry) == 0
        assert runtime.recover_stale_concurrency_leases(session) == 0
        terminated.set()
        assert drain_telegram_terminations(factory, registry=registry) == 1
        session.expire_all()
        assert runtime.recover_stale_concurrency_leases(session) == 1
        lease = session.scalar(select(AccountPoolConcurrencyLease))
        budget = session.scalar(select(AccountBehaviorBudgetReservation))
        assert lease.state == "released" and budget.state == "unknown"
        assert attempt.status == "result_unknown" and action.status == "unknown_after_send"
        assert drain_telegram_terminations(factory, registry=registry) == 0
        assert runtime.recover_stale_concurrency_leases(session) == 0


def test_failed_commit_retains_receipt_for_retry(caplog):
    class FailingCommitSession(Session):
        def commit(self):
            raise RuntimeError("injected_commit_failure")
    registry, terminated = PendingTerminations(), Event()
    terminated.set()
    with _session() as session:
        _, attempt = _unknown(session)
        register_termination(attempt, terminated, registry=registry)
        broken = sessionmaker(session.get_bind(), class_=FailingCommitSession)
        assert drain_telegram_terminations(broken, registry=registry) == 0
        session.expire_all()
        assert attempt.result_snapshot["transport_termination_state"] == "cancellation_unconfirmed"
        assert len(registry.completed()) == 1
        assert "telegram_termination_persist_failed" in caplog.text
        assert drain_telegram_terminations(sessionmaker(session.get_bind()), registry=registry) == 1


def test_receipt_cannot_modify_another_action():
    registry, terminated = PendingTerminations(), Event()
    terminated.set()
    with _session() as session:
        _, attempt = _unknown(session)
        registry.register(TerminationReceipt(attempt.tenant_id, "other-action", attempt.id, terminated))
        assert drain_telegram_terminations(sessionmaker(session.get_bind()), registry=registry) == 0
        session.refresh(attempt)
        assert attempt.result_snapshot["transport_termination_state"] == "cancellation_unconfirmed"


def test_receipt_waits_for_initial_unknown_transaction():
    registry, terminated = PendingTerminations(), Event()
    terminated.set()
    with _session() as session:
        _, attempt = _unknown(session)
        attempt.status = "gateway_call_started"
        session.commit()
        register_termination(attempt, terminated, registry=registry)
        factory = sessionmaker(session.get_bind())
        assert drain_telegram_terminations(factory, registry=registry) == 0
        attempt.status = "result_unknown"
        session.commit()
        assert drain_telegram_terminations(factory, registry=registry) == 1


def test_unknown_boundary_registers_actual_attempt(monkeypatch):
    received, terminated = [], Event()
    monkeypatch.setattr("app.services.task_center.telegram_termination.register_termination",
                        lambda attempt, signal: received.append((attempt.id, signal)))
    with _session() as session:
        action, attempt = _unknown(session)
        attempt.status = "gateway_call_started"
        session.flush()
        dispatcher._mark_unknown_after_send(session, action, "timeout",
            transport_termination_acknowledged=False, termination_event=terminated)
        assert received == [(attempt.id, terminated)]


def test_duplicate_registration_keeps_same_identity():
    registry = PendingTerminations()
    signal = Event()
    receipt = TerminationReceipt(1, "action", "attempt", signal)
    registry.register(receipt)
    registry.register(receipt)
    with pytest.raises(ValueError, match="identity_conflict"):
        registry.register(TerminationReceipt(1, "other", "attempt", signal))
    signal.set()
    assert registry.completed() == (receipt,)


def test_dispatch_exception_forwards_runner_signal(monkeypatch):
    signal, received = Event(), []
    error = TelethonOperationTimeout(transport_termination_acknowledged=False, termination_event=signal)
    def dispatch(*_args):
        raise error
    monkeypatch.setattr(dispatcher, "_legacy_review_enabled", lambda: False)
    monkeypatch.setattr(dispatcher, "_action_pre_dispatch_handled", lambda *_: False)
    monkeypatch.setattr(dispatcher, "_dispatch_account", lambda *_: object())
    monkeypatch.setattr(dispatcher, "validate_action_payload", lambda *_: object())
    monkeypatch.setattr(dispatcher, "_dispatch_validated_action", dispatch)
    monkeypatch.setattr(dispatcher, "_gateway_call_started", lambda *_: True)
    monkeypatch.setattr(dispatcher, "_mark_unknown_after_send", lambda *args, **kwargs: received.append(kwargs))
    action = SimpleNamespace(id="action", action_type="like_message", payload={})
    assert dispatcher._dispatch_action(None, action, generation_dependencies=None, comment_generation_dependencies=None)
    assert received[0]["termination_event"] is signal
    assert received[0]["transport_termination_acknowledged"] is False


@pytest.mark.parametrize("status", ["success", "failed"])
def test_late_receipt_does_not_overwrite_business_terminal(status):
    registry, signal = PendingTerminations(), Event()
    signal.set()
    with _session() as session:
        action, attempt = _unknown(session)
        action.status = attempt.status = status
        runtime.settle_attempt_resources(attempt, action, remote_mutation_started=True)
        session.commit()
        register_termination(attempt, signal, registry=registry)
        assert drain_telegram_terminations(sessionmaker(session.get_bind()), registry=registry) == 1
        session.expire_all()
        assert attempt.status == action.status == status
        assert runtime.recover_stale_concurrency_leases(session) == 0
