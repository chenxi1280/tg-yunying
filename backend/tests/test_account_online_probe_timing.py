from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import AccountHealth
from app.models import AccountStatus, TgAccount, TgAccountOnlineState
from app.services._common import _now
from app.services.account_online_constants import (
    ONLINE_LOW_FREQUENCY_PROBE_INTERVAL,
    ONLINE_PROBE_FAILURE_RETRY_AFTER,
    ONLINE_PROBE_INTERVAL,
)
from app.services.account_online_probe import OnlineProbeJob, OnlineProbeResult, _run_health_probes
from app.services.account_online_state import mark_stale_online_states, probe_due_online_states


pytestmark = pytest.mark.no_postgres

MIN_ACTIVE_PROBE_INTERVAL = timedelta(minutes=5)
MIN_ACTIVE_STALE_WINDOW = timedelta(minutes=15)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _account(session: Session, account_id: int = 101) -> TgAccount:
    account = TgAccount(
        id=account_id,
        tenant_id=1,
        display_name=f"账号{account_id}",
        phone_masked=f"138****{account_id}",
        status=AccountStatus.ACTIVE.value,
        session_ciphertext="session",
        proxy_id=7,
    )
    session.add(account)
    session.commit()
    return account


def test_probe_due_online_states_keeps_stale_window_after_next_probe(monkeypatch):
    now = _now()
    with _session() as session:
        _account(session)
        state = TgAccountOnlineState(
            tenant_id=1,
            account_id=101,
            desired_online=True,
            desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
            online_status="warming",
            next_probe_at=now - timedelta(seconds=1),
        )
        session.add(state)
        session.commit()

        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(
            "app.services.account_online_probe.gateway.check_account_health",
            lambda _session_ciphertext, _credentials: AccountHealth(status=AccountStatus.ACTIVE.value, health_score=96, detail="账号 session 可用"),
        )

        assert probe_due_online_states(session, limit=10, now=now) == 1
        session.commit()

        assert state.next_probe_at == now + ONLINE_PROBE_INTERVAL
        assert state.stale_after_at > state.next_probe_at
        assert state.next_probe_at >= now + MIN_ACTIVE_PROBE_INTERVAL
        assert state.stale_after_at >= now + MIN_ACTIVE_STALE_WINDOW


def test_probe_due_online_states_schedules_from_probe_completion(monkeypatch):
    started_at = _now()
    completed_at = started_at + timedelta(minutes=8)
    clock = iter([started_at, completed_at, completed_at])
    with _session() as session:
        _account(session)
        state = TgAccountOnlineState(
            tenant_id=1,
            account_id=101,
            desired_online=True,
            desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
            online_status="warming",
            next_probe_at=started_at - timedelta(seconds=1),
        )
        session.add(state)
        session.commit()

        monkeypatch.setattr("app.services.account_online_probe._now", lambda: next(clock))
        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(
            "app.services.account_online_probe.gateway.check_account_health_isolated",
            lambda *_args: AccountHealth(status=AccountStatus.ACTIVE.value, health_score=96, detail="账号 session 可用"),
        )

        assert probe_due_online_states(session, limit=10) == 1
        session.commit()

        assert state.last_probe_at == completed_at
        assert state.next_probe_at == completed_at + ONLINE_PROBE_INTERVAL


def test_probe_due_online_states_preserves_worker_completion_and_schedules_after_consume(monkeypatch):
    started_at = _now()
    completed_at = started_at + timedelta(minutes=2)
    consumed_at = started_at + timedelta(minutes=8)
    clock = iter([started_at, consumed_at])
    with _session() as session:
        _account(session)
        state = TgAccountOnlineState(
            tenant_id=1,
            account_id=101,
            desired_online=True,
            desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
            online_status="warming",
            next_probe_at=started_at - timedelta(seconds=1),
        )
        session.add(state)
        session.commit()

        result = OnlineProbeResult(
            account_id=101,
            health=AccountHealth(status=AccountStatus.ACTIVE.value, health_score=96, detail="账号 session 可用"),
            completed_at=completed_at,
        )
        monkeypatch.setattr("app.services.account_online_probe._now", lambda: next(clock))
        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr("app.services.account_online_probe._run_health_probes", lambda _jobs: iter([result]))

        assert probe_due_online_states(session, limit=10) == 1
        session.commit()

        assert state.last_probe_at == completed_at
        assert state.next_probe_at == consumed_at + ONLINE_PROBE_INTERVAL


def test_probe_batch_defers_early_accounts_until_after_last_completion(monkeypatch):
    started_at = _now()
    first_completed_at = started_at + timedelta(minutes=1)
    batch_completed_at = started_at + timedelta(minutes=8)
    with _session() as session:
        _account(session, 101)
        _account(session, 102)
        states = [
            TgAccountOnlineState(
                tenant_id=1,
                account_id=account_id,
                desired_online=True,
                desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
                online_status="warming",
                next_probe_at=started_at - timedelta(seconds=1),
            )
            for account_id in (101, 102, 103)
        ]
        session.add_all(states)
        session.commit()
        results = [
            OnlineProbeResult(
                account_id=101,
                health=AccountHealth(status=AccountStatus.ACTIVE.value, health_score=96, detail="ok"),
                completed_at=first_completed_at,
            ),
            OnlineProbeResult(
                account_id=102,
                health=AccountHealth(status=AccountStatus.ACTIVE.value, health_score=96, detail="ok"),
                completed_at=batch_completed_at,
            ),
        ]
        monkeypatch.setattr("app.services.account_online_probe._now", lambda: started_at)
        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr("app.services.account_online_probe._run_health_probes", lambda _jobs: iter(results))

        assert probe_due_online_states(session, limit=10) == 3
        session.commit()

        assert states[0].last_probe_at == first_completed_at
        assert states[1].last_probe_at == batch_completed_at
        assert {state.next_probe_at for state in states[:2]} == {
            batch_completed_at + ONLINE_PROBE_INTERVAL,
        }
        assert states[2].last_probe_at == started_at
        assert states[2].next_probe_at == batch_completed_at + ONLINE_PROBE_FAILURE_RETRY_AFTER


def test_probe_batch_closes_database_transaction_before_telegram_calls(monkeypatch):
    now = _now()
    with _session() as session:
        _account(session)
        session.add(TgAccountOnlineState(
            tenant_id=1,
            account_id=101,
            desired_online=True,
            desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
            online_status="warming",
            next_probe_at=now - timedelta(seconds=1),
        ))
        session.commit()

        def _results(_jobs):
            assert not session.in_transaction()
            return iter([OnlineProbeResult(
                account_id=101,
                error=TimeoutError(),
                completed_at=now,
            )])

        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr("app.services.account_online_probe._run_health_probes", _results)

        assert probe_due_online_states(session, limit=10, now=now, commit_each=True) == 1


def test_probe_batch_does_not_reload_expired_probe_objects_between_results(monkeypatch):
    now = _now()
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    statements_after_probe_start: list[str] = []
    probe_started = False

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if probe_started:
            statements_after_probe_start.append(statement)

    with Session(engine) as session:
        for account_id in (101, 102):
            _account(session, account_id)
            session.add(TgAccountOnlineState(
                tenant_id=1,
                account_id=account_id,
                desired_online=True,
                desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
                online_status="warming",
                next_probe_at=now - timedelta(seconds=1),
            ))
        session.commit()

        def _results(_jobs):
            nonlocal probe_started
            probe_started = True
            return iter([
                OnlineProbeResult(account_id=101, error=TimeoutError(), completed_at=now),
                OnlineProbeResult(account_id=102, error=TimeoutError(), completed_at=now),
            ])

        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr("app.services.account_online_probe._run_health_probes", _results)

        assert probe_due_online_states(session, limit=10, now=now, commit_each=True) == 2

    lazy_probe_selects = [
        statement for statement in statements_after_probe_start
        if "FROM tg_accounts" in statement or "FROM tg_account_online_state" in statement
    ]
    assert lazy_probe_selects == []


def test_mark_stale_online_states_requeues_probe_immediately():
    now = _now()
    with _session() as session:
        _account(session)
        state = TgAccountOnlineState(
            tenant_id=1,
            account_id=101,
            desired_online=True,
            desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
            online_status="online",
            stale_after_at=now - timedelta(seconds=1),
            next_probe_at=now + ONLINE_LOW_FREQUENCY_PROBE_INTERVAL,
        )
        session.add(state)
        session.commit()

        assert mark_stale_online_states(session, limit=10, now=now) == 1
        session.commit()

        assert state.online_status == "offline"
        assert state.failure_type == "stale_probe"
        assert state.next_probe_at <= now


def test_drain_account_online_keepalive_defers_stale_marking_when_probe_batch_is_full(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    calls: list[str] = []

    monkeypatch.setattr("app.services.account_online_state.reconcile_runtime_online_sources", lambda _session: calls.append("reconcile") or 0)
    monkeypatch.setattr("app.services.account_online_state.probe_due_online_states", lambda _session, limit, commit_each=False: calls.append(f"probe:{limit}:commit_each={commit_each}") or limit)
    monkeypatch.setattr("app.services.account_online_state.mark_stale_online_states", lambda _session, limit: calls.append(f"stale:{limit}") or 1)

    from app.services.account_online_state import drain_account_online_keepalive

    assert drain_account_online_keepalive(lambda: Session(engine), limit=3) == 3
    assert calls == ["reconcile", "probe:3:commit_each=True"]


def test_drain_account_online_keepalive_uses_requested_probe_batch(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    calls: list[str] = []

    monkeypatch.setattr("app.services.account_online_state.reconcile_runtime_online_sources", lambda _session: calls.append("reconcile") or 0)
    monkeypatch.setattr("app.services.account_online_state.probe_due_online_states", lambda _session, limit, commit_each=False: calls.append(f"probe:{limit}:commit_each={commit_each}") or limit)
    monkeypatch.setattr("app.services.account_online_state.mark_stale_online_states", lambda _session, limit: calls.append(f"stale:{limit}") or 1)

    from app.services.account_online_state import drain_account_online_keepalive

    assert drain_account_online_keepalive(lambda: Session(engine), limit=500) == 500
    assert calls == ["reconcile", "probe:500:commit_each=True"]


def test_health_probe_results_stream_as_completed(monkeypatch):
    slow_started = threading.Event()
    release_slow = threading.Event()
    first_received = threading.Event()
    results = []

    def check_health(session_ciphertext, _credentials):
        if session_ciphertext == "slow":
            slow_started.set()
            release_slow.wait(timeout=1)
        return AccountHealth(status=AccountStatus.ACTIVE.value, health_score=95, detail="ok")

    def consume_results():
        for result in _run_health_probes([
            OnlineProbeJob(account_id=1, session_ciphertext="slow", credentials=object()),
            OnlineProbeJob(account_id=2, session_ciphertext="fast", credentials=object()),
        ]):
            results.append(result)
            first_received.set()

    monkeypatch.setattr("app.services.account_online_probe.gateway.check_account_health", check_health)
    consumer = threading.Thread(target=consume_results)
    consumer.start()
    assert slow_started.wait(timeout=1)
    try:
        assert first_received.wait(timeout=0.2)
        assert results[0].account_id == 2
    finally:
        release_slow.set()
        consumer.join(timeout=1)


def test_health_probe_uses_isolated_gateway_entry(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.account_online_probe.gateway.check_account_health_isolated",
        lambda *_args: calls.append("isolated")
        or AccountHealth(status=AccountStatus.ACTIVE.value, health_score=95, detail="ok"),
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.account_online_probe.gateway.check_account_health",
        lambda *_args: pytest.fail("account-online must not use the process-wide loop"),
    )

    results = list(
        _run_health_probes(
            [OnlineProbeJob(account_id=1, session_ciphertext="session", credentials=object())]
        )
    )

    assert calls == ["isolated"]
    assert results[0].health.status == AccountStatus.ACTIVE.value
