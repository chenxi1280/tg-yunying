from datetime import timedelta

import pytest

from app.services.task_center.engagement_legacy_occupancy import read_legacy_attempt_occupancy
from app.services.task_center.gateway_evidence_journal import (
    GatewayResultEvidence, bind_gateway_request_identity, record_gateway_result_evidence,
)
from app.services.task_center.runtime_state_hash import canonical_state_hash
from tests.test_engagement_legacy_occupancy import SCOPE, _call
from tests.test_engagement_runtime_resources import _seed, _session


pytestmark = pytest.mark.no_postgres


def _returned(session, *, legacy=False):
    task = _seed(session)
    action, attempt = _call(session, task, state="result_unknown", action_type="send_message")
    action.status = "closed_unknown"
    bind_gateway_request_identity(action, attempt)
    journal = record_gateway_result_evidence(session, action, attempt,
        GatewayResultEvidence(remote_message_id="returned-message", remote_mutation_started=True))
    if legacy:
        result = {"remote_message_id": journal.remote_message_id,
            "remote_fact_id": journal.remote_fact_id, "failure_code": journal.failure_code,
            "remote_mutation_state": journal.remote_mutation_state}
        journal.result_fingerprint = canonical_state_hash(result)
        journal.evidence_hash = canonical_state_hash({
            "gateway_request_identity": journal.gateway_request_identity,
            "request_fingerprint": journal.request_fingerprint, "result": result})
    session.flush()
    return action, attempt, journal


@pytest.mark.parametrize("legacy", [False, True])
def test_verified_gateway_return_releases_only_physical_occupancy(legacy):
    with _session() as session:
        action, attempt, journal = _returned(session, legacy=legacy)
        original = (dict(attempt.result_snapshot), journal.evidence_hash,
            action.pacing_due_at, attempt.gateway_call_started_at)
        row, = read_legacy_attempt_occupancy(session, SCOPE)
        assert not row.remote_inflight and row.issues == ()
        assert row.original_task_day == row.call_day == SCOPE.task_day
        assert action.status == "closed_unknown" and attempt.status == "result_unknown"
        assert (attempt.result_snapshot, journal.evidence_hash,
            action.pacing_due_at, attempt.gateway_call_started_at) == original


@pytest.mark.parametrize("damage", ["identity", "request", "target", "result", "evidence",
    "owner", "time", "conflict", "missing_request", "typed_added_to_legacy", "unknown"])
def test_unverified_journal_never_releases_physical_occupancy(damage):
    with _session() as session:
        action, attempt, journal = _returned(session, legacy=True)
        if damage in {"identity", "request", "target", "result", "evidence"}:
            field = {"identity": "gateway_request_identity", "request": "request_fingerprint",
                "target": "target_fingerprint", "result": "result_fingerprint",
                "evidence": "evidence_hash"}[damage]
            setattr(journal, field, "damaged")
        if damage == "owner":
            journal.account_id += 1
        if damage == "time":
            journal.observed_at = attempt.gateway_call_started_at - timedelta(seconds=1)
        if damage == "conflict":
            journal.state = "conflict"
        if damage == "missing_request":
            attempt.result_snapshot = {}
        if damage == "typed_added_to_legacy":
            journal.typed_remote_fact = {"kind": "unexpected_fact"}
        if damage == "unknown":
            journal.remote_mutation_state = "unknown"
            journal.remote_message_id = ""
        session.flush()
        row, = read_legacy_attempt_occupancy(session, SCOPE)
        assert row.remote_inflight
        assert action.status == "closed_unknown" and attempt.status == "result_unknown"


def test_unverified_other_journal_cannot_override_conflicting_evidence():
    with _session() as session:
        action, attempt, _ = _returned(session)
        from tests.test_engagement_legacy_occupancy import _journal
        _journal(session, action, attempt, state="false", suffix="-other")
        row, = read_legacy_attempt_occupancy(session, SCOPE)
        assert row.remote_inflight
        assert "remote_mutation_evidence_conflict" in row.issues
