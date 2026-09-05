from datetime import timedelta

import pytest
from app.models import ListenerSourceState, TgAccount
from app.services.task_center import channel_listener_runtime
from tests.test_channel_listener_candidate_order import (
    FIRST_UNTRIED_ID,
    LAST_ACCOUNT_ID,
    NOW,
    _seed_candidates,
    _selected,
    _session,
)


pytestmark = pytest.mark.no_postgres
OTHER_CHANNEL_ID = "32"


def _readable_state(session, account_id, **overrides):
    fields = {
        "tenant_id": 1, "source_type": "channel", "source_peer_id": OTHER_CHANNEL_ID,
        "account_id": account_id, "snapshot_status": "ready", "observed_at": NOW,
        "fresh_until_at": NOW + timedelta(minutes=1),
    }
    session.add(ListenerSourceState(**{**fields, **overrides}))
    session.flush()


def test_recent_reader_is_selected_before_sql_candidate_limit(monkeypatch):
    reader_id = LAST_ACCOUNT_ID + 1000
    with _session(monkeypatch) as session:
        task = _seed_candidates(session, last_id=reader_id)
        _readable_state(session, reader_id)
        assert _selected(session, task) == reader_id


def test_recent_reader_priority_survives_recent_target_candidate_merge(monkeypatch):
    with _session(monkeypatch) as session:
        _seed_candidates(session)
        _readable_state(session, LAST_ACCOUNT_ID)
        accounts = [session.get(TgAccount, account_id)
                    for account_id in (FIRST_UNTRIED_ID, LAST_ACCOUNT_ID)]
        selected = channel_listener_runtime._preferred_listener_account(
            session, channel_target_id=31, accounts=accounts,
        )
        assert selected.id == LAST_ACCOUNT_ID


@pytest.mark.parametrize("overrides", [
    {"tenant_id": 2}, {"source_type": "group"}, {"snapshot_status": "unavailable"},
    {"observed_at": None}, {"observed_at": NOW + timedelta(seconds=1)},
    {"fresh_until_at": NOW}, {"fresh_until_at": None},
])
def test_only_current_same_tenant_channel_read_evidence_is_ranked(monkeypatch, overrides):
    with _session(monkeypatch) as session:
        task = _seed_candidates(session)
        _readable_state(session, LAST_ACCOUNT_ID, **overrides)
        assert _selected(session, task) == FIRST_UNTRIED_ID


@pytest.mark.parametrize("current_state", ["ready", "unavailable"])
def test_other_channel_read_does_not_override_current_channel_state(monkeypatch, current_state):
    with _session(monkeypatch) as session:
        task = _seed_candidates(session)
        _readable_state(session, LAST_ACCOUNT_ID)
        session.add(ListenerSourceState(
            tenant_id=1, source_type="channel", source_peer_id="31",
            account_id=LAST_ACCOUNT_ID, snapshot_status=current_state,
            next_probe_at=NOW - timedelta(seconds=1), updated_at=NOW,
        ))
        session.flush()
        expected = LAST_ACCOUNT_ID if current_state == "ready" else FIRST_UNTRIED_ID
        assert _selected(session, task) == expected


def test_recent_reader_remains_inside_original_task_account_scope(monkeypatch):
    with _session(monkeypatch) as session:
        task = _seed_candidates(session)
        _readable_state(session, LAST_ACCOUNT_ID)
        task.account_config = {"selection_mode": "manual", "account_ids": [FIRST_UNTRIED_ID]}
        session.flush()
        assert _selected(session, task) == FIRST_UNTRIED_ID
