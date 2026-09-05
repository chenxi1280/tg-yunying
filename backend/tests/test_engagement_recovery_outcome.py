from datetime import timedelta

import pytest
from sqlalchemy.orm.attributes import flag_modified

from app.services._common import _now
from app.services.task_center import engagement_runtime_resources as runtime
from app.services.task_center.dispatcher import _release_dangling_engagement_leases
from app.services.task_center.gateway_evidence_journal import (
    GatewayResultEvidence, bind_gateway_request_identity, record_gateway_result_evidence,
)
from tests.test_engagement_recovery_regressions import _inflight, _resources
from tests.test_engagement_runtime_resources import _seed, _session


pytestmark = pytest.mark.no_postgres


def _failed(session, *, mutation, journal=False):
    task = _seed(session)
    action, attempt = _inflight(session, task, 11)
    action.status = attempt.status = "failed"
    attempt.after_call_at = _now()
    attempt.result_snapshot = {**attempt.result_snapshot, "remote_mutation_started": mutation}
    receipt = None
    if journal:
        bind_gateway_request_identity(action, attempt)
        receipt = record_gateway_result_evidence(session, action, attempt,
            GatewayResultEvidence(failure_code="terminal_failed", remote_mutation_started=mutation))
        attempt.result_snapshot = {**attempt.result_snapshot, "remote_mutation_started": False}
    session.flush()
    return action, attempt, receipt


@pytest.mark.parametrize("mutation", [True, False])
@pytest.mark.parametrize("journal", [True, False])
@pytest.mark.parametrize("entry", ["recovery", "dispatcher"])
def test_recovered_cost_uses_original_result_not_call_start(mutation, journal, entry):
    with _session() as session:
        action, attempt, _ = _failed(session, mutation=mutation, journal=journal)
        if entry == "recovery":
            assert runtime.recover_stale_concurrency_leases(session) == 1
        else:
            _release_dangling_engagement_leases(session, action)
        lease, budget, fence = _resources(session, attempt)
        assert lease.state == "released" and fence.state == "terminal"
        assert budget.state == ("confirmed" if mutation else "released")
        assert fence.business_outcome_state == ("failed" if mutation else "safely_not_called")
        assert action.status == attempt.status == "failed"
        assert runtime.recover_stale_concurrency_leases(session) == 0


@pytest.mark.parametrize("damage", ["none", "string", "number", "no_after", "journal_unknown",
    "owner", "epoch", "hash", "identity", "time", "conflict"])
def test_unproven_called_failure_keeps_resources_and_reports_error(damage, caplog):
    with _session() as session:
        journal_case = damage not in {"none", "string", "number", "no_after", "epoch"}
        action, attempt, receipt = _failed(session, mutation=False, journal=journal_case)
        if damage in {"none", "string", "number"}:
            attempt.result_snapshot = {**attempt.result_snapshot,
                "remote_mutation_started": {"none": None, "string": "false", "number": 0}[damage]}
        if damage == "no_after":
            attempt.after_call_at = None
        if damage == "epoch":
            attempt.task_lifecycle_epoch += 1
        if damage == "journal_unknown":
            receipt.remote_mutation_state = "unknown"
        if damage == "owner":
            receipt.account_id += 1
        if damage == "hash":
            receipt.evidence_hash = "damaged"
        if damage == "identity":
            receipt.gateway_request_identity = "different-request"
        if damage == "time":
            receipt.observed_at = attempt.gateway_call_started_at - timedelta(seconds=1)
        if damage == "conflict":
            receipt.state = "conflict"
        # Python dict equality treats False == 0; persist the intended JSON type.
        flag_modified(attempt, "result_snapshot")
        assert runtime.recover_stale_concurrency_leases(session) == 0
        lease, budget, fence = _resources(session, attempt)
        assert (lease.state, budget.state, fence.state) == ("call_issued", "call_issued", "active")
        assert fence.transport_termination_state != "acknowledged"
        assert action.status == attempt.status == "failed"
        assert "engagement_recovery_outcome_unproven" in caplog.text


def test_direct_settlement_cannot_release_called_failure_without_outcome():
    with _session() as session:
        action, attempt, _ = _failed(session, mutation=None)
        with pytest.raises(RuntimeError, match="engagement_recovery_outcome_unproven"):
            runtime.settle_attempt_resources(attempt, action, remote_mutation_started=None)
        lease, budget, fence = _resources(session, attempt)
        assert (lease.state, budget.state, fence.state) == ("call_issued", "call_issued", "active")
