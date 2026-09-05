from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountStatus, ListenerSourceState, OperationTarget, Task, TgAccount
from app.services.task_center import channel_listener_accounts, channel_listener_runtime
from tests.test_channel_listener_snapshot import _seed_listener_task


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 5, 15, 0)
TASK_ID = "listener-full-scope"
FIRST_ACCOUNT_ID = 101
LAST_ACCOUNT_ID = 145
FIRST_UNTRIED_ID = 141


def _session(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(channel_listener_runtime, "_now", lambda: NOW)
    monkeypatch.setattr(channel_listener_accounts, "_now", lambda: NOW)
    _seed_listener_task(engine, task_id=TASK_ID, task_type="channel_view",
                        channel_id=31, account_id=FIRST_ACCOUNT_ID)
    return Session(engine)


def _seed_candidates(session, *, failed_until=FIRST_UNTRIED_ID, last_id=LAST_ACCOUNT_ID):
    task = session.get(Task, TASK_ID)
    ids = list(range(FIRST_ACCOUNT_ID, last_id + 1))
    task.account_config = {"selection_mode": "manual", "account_ids": ids}
    session.add_all([TgAccount(id=account_id, tenant_id=1,
        display_name=f"reader-{account_id}", phone_masked=f"***{account_id}",
        status=AccountStatus.ACTIVE.value, session_ciphertext=f"test-{account_id}")
        for account_id in ids if account_id != FIRST_ACCOUNT_ID])
    session.add_all([ListenerSourceState(tenant_id=1, source_type="channel",
        source_peer_id="31", account_id=account_id, snapshot_status="unavailable",
        next_probe_at=NOW - timedelta(seconds=1), updated_at=NOW)
        for account_id in ids if account_id < failed_until])
    session.flush()
    return task


def _selected(session, task):
    return channel_listener_runtime._source_for_task(
        session, task, session.get(OperationTarget, 31)).account_id


def test_listener_reaches_untried_account_beyond_first_candidate_page(monkeypatch):
    with _session(monkeypatch) as session:
        task = _seed_candidates(session)
        assert _selected(session, task) == FIRST_UNTRIED_ID


def test_listener_keeps_ready_observer_outside_first_candidate_page(monkeypatch):
    with _session(monkeypatch) as session:
        task = _seed_candidates(session)
        session.add(ListenerSourceState(tenant_id=1, source_type="channel",
            source_peer_id="31", account_id=LAST_ACCOUNT_ID, snapshot_status="ready",
            updated_at=NOW))
        session.flush()
        assert _selected(session, task) == LAST_ACCOUNT_ID


def test_listener_retries_oldest_failure_across_complete_scope(monkeypatch):
    with _session(monkeypatch) as session:
        task = _seed_candidates(session, failed_until=LAST_ACCOUNT_ID + 1)
        for row in session.query(ListenerSourceState).all():
            if row.account_id == LAST_ACCOUNT_ID:
                row.updated_at = NOW - timedelta(hours=1)
        session.flush()
        assert _selected(session, task) == LAST_ACCOUNT_ID


def test_listener_order_keeps_original_account_scope(monkeypatch):
    with _session(monkeypatch) as session:
        task = _seed_candidates(session)
        task.account_config = {"selection_mode": "manual", "account_ids": [101]}
        session.add(ListenerSourceState(tenant_id=1, source_type="channel",
            source_peer_id="31", account_id=LAST_ACCOUNT_ID, snapshot_status="ready"))
        session.flush()
        assert _selected(session, task) == FIRST_ACCOUNT_ID


def test_listener_runtime_compares_offset_timestamps_by_instant():
    instant = datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc)
    assert channel_listener_runtime._wall(instant) == NOW


@pytest.mark.parametrize("scope", [
    {"tenant_id": 2, "source_type": "channel", "source_peer_id": "31"},
    {"tenant_id": 1, "source_type": "channel", "source_peer_id": "32"},
    {"tenant_id": 1, "source_type": "group", "source_peer_id": "31"},
])
def test_unrelated_source_states_do_not_override_listener_order(monkeypatch, scope):
    with _session(monkeypatch) as session:
        task = _seed_candidates(session)
        session.add(ListenerSourceState(**scope, account_id=LAST_ACCOUNT_ID,
                                        snapshot_status="ready"))
        session.flush()
        assert _selected(session, task) == FIRST_UNTRIED_ID


def test_listener_candidate_queries_do_not_grow_with_account_count(monkeypatch):
    query_counts = []
    for last_id in (LAST_ACCOUNT_ID, LAST_ACCOUNT_ID + 1000):
        with _session(monkeypatch) as session:
            task = _seed_candidates(session, last_id=last_id)
            statements = []

            def record_select(_conn, _cursor, statement, _params, _ctx, _many):
                if statement.lstrip().upper().startswith("SELECT"):
                    statements.append(statement)

            event.listen(session.bind, "before_cursor_execute", record_select)
            assert _selected(session, task) == FIRST_UNTRIED_ID
            event.remove(session.bind, "before_cursor_execute", record_select)
            query_counts.append(len(statements))
    assert query_counts[0] == query_counts[1]
