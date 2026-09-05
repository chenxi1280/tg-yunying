from datetime import timedelta

import pytest

from app.services.task_center.engagement_legacy_occupancy import read_legacy_attempt_occupancy
from tests.test_engagement_legacy_occupancy import SCOPE, _call, _journal
from tests.test_engagement_runtime_resources import _seed, _session


pytestmark = pytest.mark.no_postgres


def test_unknown_journal_does_not_fall_back_to_stale_false_snapshot():
    with _session() as session:
        action, attempt = _call(session, _seed(session), state="failed")
        attempt.result_snapshot = {"remote_mutation_started": False}
        _journal(session, action, attempt, state="unknown")
        row, = read_legacy_attempt_occupancy(session, SCOPE)
        assert row.remote_inflight and row.issues == ()


@pytest.mark.parametrize("value", [0, 1, "false", "true", None, False, True])
def test_only_original_json_boolean_can_prove_mutation(value):
    with _session() as session:
        _, attempt = _call(session, _seed(session), state="failed")
        attempt.result_snapshot = {"remote_mutation_started": value}
        session.flush()
        rows = read_legacy_attempt_occupancy(session, SCOPE)
        if value is False:
            assert rows == ()
            return
        row, = rows
        assert row.remote_inflight
        if value is not True and value is not None:
            assert "remote_mutation_snapshot_invalid" in row.issues
        else:
            assert row.issues == ()


def test_false_snapshot_before_original_call_cannot_release_occupancy():
    with _session() as session:
        _, attempt = _call(session, _seed(session), state="failed")
        attempt.result_snapshot = {"remote_mutation_started": False}
        attempt.after_call_at = attempt.gateway_call_started_at - timedelta(seconds=1)
        session.flush()
        row, = read_legacy_attempt_occupancy(session, SCOPE)
        assert row.remote_inflight


@pytest.mark.parametrize("damage", ["request", "result_hash", "evidence_hash", "time", "remote_id"])
def test_unproven_false_journal_cannot_release_occupancy(damage):
    with _session() as session:
        action, attempt = _call(session, _seed(session), state="failed")
        journal = _journal(session, action, attempt, state="false")
        if damage == "request":
            journal.request_fingerprint = "m" * 64
        elif damage == "result_hash":
            journal.result_fingerprint = "m" * 64
        elif damage == "evidence_hash":
            journal.evidence_hash = "m" * 64
        elif damage == "time":
            journal.observed_at = attempt.gateway_call_started_at - timedelta(seconds=1)
        else:
            journal.remote_message_id = "unexpected-remote-message"
        session.flush()
        row, = read_legacy_attempt_occupancy(session, SCOPE)
        assert row.remote_inflight
        assert "gateway_journal_result_unproven" in row.issues
