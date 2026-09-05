import pytest

from app.database import SessionLocal
from app.services._common import _now
from app.services.task_center import engagement_runtime_resources as runtime
from app.services.task_center import engagement_lease_recovery as recovery
from app.services.task_center.gateway_evidence_journal import (
    GatewayResultEvidence, bind_gateway_request_identity, record_gateway_result_evidence,
)
from tests.test_engagement_shared_usage_postgres import _seed
from tests.test_engagement_recovery_regressions import _resources


pytestmark = pytest.mark.allow_missing_rule_binding


@pytest.mark.parametrize("mutation", [True, False])
def test_recovery_uses_postgres_original_journal_cost_with_stale_precall_snapshot(mutation):
    with SessionLocal() as session:
        action, attempt = _seed(session)
        runtime.reserve_attempt_resources(session, action, attempt)
        runtime.mark_attempt_call_issued(session, attempt)
        bind_gateway_request_identity(action, attempt)
        action.status = attempt.status = "failed"
        attempt.after_call_at = _now()
        attempt.result_snapshot = {**attempt.result_snapshot, "remote_mutation_started": False}
        record_gateway_result_evidence(session, action, attempt,
            GatewayResultEvidence(failure_code="terminal_failed", remote_mutation_started=mutation))
        session.flush()
        session.expire_all()
        assert runtime.recover_stale_concurrency_leases(session) == 1
        lease, budget, fence = _resources(session, attempt)
        assert lease.state == "released" and fence.state == "terminal"
        assert budget.state == ("confirmed" if mutation else "released")
        assert fence.business_outcome_state == ("failed" if mutation else "safely_not_called")
        assert attempt.status == "failed"
        assert runtime.recover_stale_concurrency_leases(session) == 0
        session.rollback()


@pytest.mark.parametrize("mutation", [0, None, "false"])
def test_postgres_nonboolean_failure_evidence_is_not_safely_not_called(mutation, caplog, monkeypatch):
    # The Alembic test bootstrap disables loggers imported before migration.
    monkeypatch.setattr(recovery.logger, "disabled", False)
    with SessionLocal() as session:
        action, attempt = _seed(session)
        runtime.reserve_attempt_resources(session, action, attempt)
        runtime.mark_attempt_call_issued(session, attempt)
        action.status = attempt.status = "failed"
        attempt.after_call_at = _now()
        attempt.result_snapshot = {**attempt.result_snapshot, "remote_mutation_started": mutation}
        session.flush()
        session.expire_all()
        assert type(attempt.result_snapshot["remote_mutation_started"]) is type(mutation)
        assert runtime.recover_stale_concurrency_leases(session) == 0
        lease, budget, fence = _resources(session, attempt)
        assert (lease.state, budget.state, fence.state) == ("call_issued", "call_issued", "active")
        assert "engagement_recovery_outcome_unproven" in caplog.text
        session.rollback()
