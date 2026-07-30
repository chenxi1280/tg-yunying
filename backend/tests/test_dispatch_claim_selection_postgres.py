from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

from sqlalchemy import delete, text

from app.database import Base, SessionLocal, engine
from app.models import DispatchClaimScope
from app.services.task_center import dispatcher
from app.services.task_center.dispatch_reservations import (
    lock_dispatch_claim_selection,
)


LOCK_SCOPE = "pg_dispatch_selection_lock"
EMPTY_SCOPE = "pg_dispatch_empty_claim"


def test_postgres_scope_lock_serializes_global_candidate_scans() -> None:
    Base.metadata.create_all(engine)
    _delete_scope(LOCK_SCOPE)
    settings = _settings(LOCK_SCOPE)
    first_acquired = Event()
    release_first = Event()
    second_started = Event()
    second_acquired = Event()

    try:
        _create_scope(settings)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                _hold_scope_lock,
                settings,
                first_acquired,
                release_first,
            )
            assert first_acquired.wait(timeout=5)
            second = pool.submit(
                _await_scope_lock,
                settings,
                second_started,
                second_acquired,
            )
            assert second_started.wait(timeout=5)
            assert not second_acquired.wait(timeout=0.3)
            release_first.set()
            first.result(timeout=5)
            second.result(timeout=5)
            assert second_acquired.is_set()
    finally:
        release_first.set()
        _delete_scope(LOCK_SCOPE)


def test_postgres_empty_claim_commits_and_releases_scope_lock(
    monkeypatch,
) -> None:
    Base.metadata.create_all(engine)
    _delete_scope(EMPTY_SCOPE)
    settings = _settings(EMPTY_SCOPE)
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)

    try:
        with SessionLocal() as first:
            assert dispatcher.claim_actions(
                first,
                limit=1,
                worker_id="empty-claim",
            ) == []
            with SessionLocal() as second:
                second.execute(text("SET LOCAL lock_timeout = '500ms'"))
                lock_dispatch_claim_selection(second, settings, 1)
                second.rollback()
    finally:
        _delete_scope(EMPTY_SCOPE)


def _settings(scope: str):
    return SimpleNamespace(
        action_claim_limit=2,
        dispatcher_claim_scope=scope,
        dispatcher_concurrency=2,
        dispatcher_scope_capacity=2,
        account_shard_total=1,
        account_shard_index=0,
    )


def _create_scope(settings) -> None:
    with SessionLocal() as session:
        lock_dispatch_claim_selection(session, settings, 2)
        session.commit()


def _hold_scope_lock(
    settings,
    acquired: Event,
    release: Event,
) -> None:
    with SessionLocal() as session:
        lock_dispatch_claim_selection(session, settings, 2)
        acquired.set()
        assert release.wait(timeout=5)
        session.commit()


def _await_scope_lock(
    settings,
    started: Event,
    acquired: Event,
) -> None:
    with SessionLocal() as session:
        started.set()
        lock_dispatch_claim_selection(session, settings, 2)
        acquired.set()
        session.rollback()


def _delete_scope(scope: str) -> None:
    with SessionLocal() as session:
        session.execute(
            delete(DispatchClaimScope).where(
                DispatchClaimScope.dispatcher_scope == scope,
            )
        )
        session.commit()
