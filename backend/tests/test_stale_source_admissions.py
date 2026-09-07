from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (Action, AuditLog, ExecutionAttempt, FulfillmentRemoteFact,
                        GatewayRequestEvidenceJournal, SourcePacingAdmission, SourcePacingState, Task, Tenant)
from app.services.task_center.stale_source_admissions import (
    RecoveryScope, apply_stale_admissions, preview_stale_admissions,
)


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 7, 10)
SCOPE = RecoveryScope(1, ("state",))


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="recovery"))
        current.add(Task(id="task", tenant_id=1, name="recovery", type="channel_like", status="running"))
        current.add(SourcePacingState(id="state", tenant_id=1, pacing_domain="reaction",
                                     source_key_hash="a" * 64, next_call_not_before_at=NOW + timedelta(days=3)))
        current.commit()
        yield current
    engine.dispose()


def reservation(session, name, *, status="failed", offset=86400):
    action = Action(id=name, tenant_id=1, task_id="task", task_type="channel_like",
                    action_type="like_message", status=status, scheduled_at=NOW + timedelta(seconds=offset))
    session.add(action)
    session.flush()
    admission = SourcePacingAdmission(
        id=f"admission-{name}", admission_key=name, tenant_id=1, task_id="task",
        source_pacing_state_id="state", owner_type="reaction_fulfillment_obligations", owner_id=name,
        action_id=name, pacing_period_key="2026-09-07", pacing_plan_hash="b" * 64,
        planned_release_at=NOW, call_not_before_at=NOW + timedelta(seconds=offset),
        source_gap_seconds=864, state="reserved",
    )
    session.add(admission)
    session.commit()
    return action, admission


def attempt(session, action, *, started=None, status="skipped_before_gateway", ended=NOW):
    row = ExecutionAttempt(tenant_id=1, action_id=action.id, attempt_no=1, before_call_at=NOW,
                           after_call_at=ended, gateway_call_started_at=started, status=status)
    session.add(row)
    session.flush()
    return row


def test_preview_is_read_only_and_predicts_actual_tail(session):
    _, stale = reservation(session, "old")
    _, future = reservation(session, "valid", status="pending", offset=3600)
    statements = []
    listener = lambda conn, cursor, statement, parameters, context, many: statements.append(statement)
    event.listen(session.get_bind(), "before_cursor_execute", listener)
    preview = preview_stale_admissions(session, SCOPE)
    event.remove(session.get_bind(), "before_cursor_execute", listener)
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    assert preview["candidate_ids"] == [stale.id]
    expected = (NOW + timedelta(seconds=3600 + 864)).isoformat()
    assert preview["states_after"]["state"] == expected
    assert preview["states_before"]["state"] != expected
    receipt = apply_stale_admissions(session, preview, actor="test", audit_reference="review-5")
    session.commit()
    assert receipt["states_after"]["state"] == expected
    assert stale.state == "cancelled_pre_gateway"
    assert future.state == "reserved"
    assert future.source_gap_seconds == 864
    audit = session.scalar(select(AuditLog))
    assert audit.actor == "test" and "review-5" in audit.detail


@pytest.mark.parametrize("status", ["pending", "claiming", "executing", "retryable_failed", "unknown_after_send", "success"])
def test_live_retryable_or_unknown_actions_never_cancelled(session, status):
    _, admission = reservation(session, "protected", status=status)
    preview = preview_stale_admissions(session, SCOPE)
    assert preview["candidate_ids"] == []
    assert admission.state == "reserved"


@pytest.mark.parametrize("started,status,ended", [
    (NOW, "failed", NOW), (None, "result_unknown", NOW), (None, "before_call", None),
])
def test_started_unknown_and_unfinished_attempts_are_protected(session, started, status, ended):
    action, admission = reservation(session, "protected")
    row = attempt(session, action, started=started, status=status, ended=ended)
    admission.attempt_id = row.id
    session.commit()
    assert preview_stale_admissions(session, SCOPE)["candidate_ids"] == []


def test_terminal_pre_gateway_attempt_can_be_recovered(session):
    action, admission = reservation(session, "safe")
    row = attempt(session, action)
    admission.attempt_id = row.id
    session.commit()
    assert preview_stale_admissions(session, SCOPE)["candidate_ids"] == [admission.id]


@pytest.mark.parametrize("evidence_kind", ["fact", "journal"])
def test_remote_evidence_blocks_recovery_even_if_attempt_timestamp_is_missing(session, evidence_kind):
    action, admission = reservation(session, "protected")
    row = attempt(session, action)
    admission.attempt_id = row.id
    if evidence_kind == "fact":
        evidence = FulfillmentRemoteFact(
            tenant_id=1, task_type=action.task_type, task_id=action.task_id,
            obligation_type="reaction", obligation_id="owner", action_id=action.id, attempt_id=row.id,
            mutation_kind="reaction", remote_mutation_key_hash="a" * 64, gateway_request_hash="b" * 64,
            fact_kind="reaction_observed", fact_identity_hash="c" * 64,
        )
    else:
        evidence = GatewayRequestEvidenceJournal(
            tenant_id=1, action_id=action.id, execution_attempt_id=row.id,
            gateway_request_identity="request", request_fingerprint="a" * 64,
            target_fingerprint="b" * 64, result_fingerprint="c" * 64, evidence_hash="d" * 64,
        )
    session.add(evidence)
    session.commit()
    assert preview_stale_admissions(session, SCOPE)["candidate_ids"] == []


@pytest.mark.parametrize("changed", ["action", "state", "admission", "attempt"])
def test_apply_rejects_drift_before_any_write(session, changed):
    action, admission = reservation(session, "safe")
    row = attempt(session, action)
    admission.attempt_id = row.id
    session.commit()
    preview = preview_stale_admissions(session, SCOPE)
    if changed == "action":
        action.action_version += 1
    elif changed == "state":
        session.get(SourcePacingState, "state").version += 1
    elif changed == "admission":
        admission.version += 1
    else:
        row.gateway_call_started_at = NOW
    session.commit()
    with pytest.raises(ValueError, match="preview_drift"):
        apply_stale_admissions(session, preview, actor="test", audit_reference="drift")
    session.rollback()
    assert admission.state == "reserved"
    assert session.scalar(select(AuditLog)) is None


def test_preview_requires_exact_tenant_and_scope(session):
    with pytest.raises(ValueError, match="nonempty"):
        RecoveryScope(1, ())
    with pytest.raises(ValueError, match="outside tenant"):
        preview_stale_admissions(session, RecoveryScope(2, ("state",)))


def test_apply_rejects_modified_preview(session):
    reservation(session, "safe")
    preview = preview_stale_admissions(session, SCOPE)
    preview["candidate_ids"] = []
    with pytest.raises(ValueError, match="preview hash"):
        apply_stale_admissions(session, preview, actor="test", audit_reference="tamper")


def test_reconcile_preserves_other_frozen_admissions_and_last_call(session):
    reservation(session, "safe")
    state = session.get(SourcePacingState, "state")
    state.last_call_started_at = NOW
    state.last_source_gap_seconds = 86400
    session.commit()
    preview = preview_stale_admissions(session, SCOPE)
    assert preview["states_after"]["state"] == (NOW + timedelta(days=1)).isoformat()
    apply_stale_admissions(session, preview, actor="test", audit_reference="gap")
    session.commit()
    assert state.last_source_gap_seconds == 86400
    assert state.next_call_not_before_at == NOW + timedelta(days=1)
