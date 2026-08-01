from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import pytest

from app.database import Base
from app.models import (
    Action,
    AuditLog,
    ExecutionAttempt,
    RemoteReconcileCase,
    Task,
    Tenant,
)
from app.services._common import _now
from app.services.task_center.remote_reconciliation import (
    RemoteReconcileEvidence,
    apply_remote_reconcile_evidence,
    ensure_remote_reconcile_case,
)


pytestmark = pytest.mark.no_postgres
EVIDENCE_FINGERPRINT = "a" * 64


def test_exact_remote_fact_confirms_once_and_replay_is_zero_write() -> None:
    engine = _engine()
    with Session(engine) as session:
        action, attempt, case = _seed_case(session)
        evidence = RemoteReconcileEvidence(
            result="remote_confirmed",
            source="telegram_history_read_only",
            evidence_fingerprint=EVIDENCE_FINGERPRINT,
            remote_message_id="remote-9988",
            exact_match_count=1,
        )

        first = apply_remote_reconcile_evidence(
            session, case.id, evidence, actor="release-workflow",
        )
        second = apply_remote_reconcile_evidence(
            session, case.id, evidence, actor="release-workflow",
        )

        assert first.changed is True
        assert second.changed is False
        assert action.status == "success"
        assert attempt.status == "success"
        assert attempt.remote_message_id == "remote-9988"
        assert case.state == "remote_confirmed"
        assert session.scalar(select(func.count(AuditLog.id))) == 1


def test_history_absence_without_authoritative_no_mutation_is_rejected() -> None:
    engine = _engine()
    with Session(engine) as session:
        _, _, case = _seed_case(session)
        evidence = RemoteReconcileEvidence(
            result="remote_absence_proven",
            source="telegram_history_read_only",
            evidence_fingerprint=EVIDENCE_FINGERPRINT,
            remote_mutation_started=None,
        )

        with pytest.raises(ValueError, match="authoritative_absence"):
            apply_remote_reconcile_evidence(
                session, case.id, evidence, actor="release-workflow",
            )


def test_authoritative_absence_replans_original_obligation_once() -> None:
    engine = _engine()
    with Session(engine) as session:
        action, attempt, case = _seed_case(session)
        evidence = RemoteReconcileEvidence(
            result="remote_absence_proven",
            source="gateway_request_journal",
            evidence_fingerprint=EVIDENCE_FINGERPRINT,
            failure_code="rpc_rejected_before_mutation",
            remote_mutation_started=False,
        )

        outcome = apply_remote_reconcile_evidence(
            session, case.id, evidence, actor="release-workflow",
        )

        assert outcome.state == "remote_absence_proven"
        assert action.status == "failed"
        assert action.result["fulfillment_replan_required"] is True
        assert attempt.status == "failed"


def test_inconclusive_keeps_unknown_hold_and_accepts_stronger_later_fact() -> None:
    engine = _engine()
    with Session(engine) as session:
        action, _, case = _seed_case(session)
        inconclusive = RemoteReconcileEvidence(
            result="inconclusive",
            source="telegram_history_timeout",
            evidence_fingerprint=EVIDENCE_FINGERPRINT,
        )
        apply_remote_reconcile_evidence(
            session, case.id, inconclusive, actor="release-workflow",
        )

        confirmed = RemoteReconcileEvidence(
            result="remote_confirmed",
            source="telegram_history_read_only",
            evidence_fingerprint="b" * 64,
            remote_message_id="remote-later",
            exact_match_count=1,
        )
        outcome = apply_remote_reconcile_evidence(
            session, case.id, confirmed, actor="release-workflow",
        )

        assert outcome.state == "remote_confirmed"
        assert action.status == "success"


def test_state_hash_drift_quarantines_without_business_mutation() -> None:
    engine = _engine()
    with Session(engine) as session:
        action, _, case = _seed_case(session)
        action.status = "failed"
        evidence = RemoteReconcileEvidence(
            result="remote_confirmed",
            source="telegram_history_read_only",
            evidence_fingerprint=EVIDENCE_FINGERPRINT,
            remote_message_id="remote-1",
            exact_match_count=1,
        )

        outcome = apply_remote_reconcile_evidence(
            session, case.id, evidence, actor="release-workflow",
        )

        assert outcome.state == "conflict"
        assert action.status == "failed"
        assert case.state == "conflict"
        assert session.scalar(select(func.count(AuditLog.id))) == 1


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed_case(session: Session):
    observed_at = _now()
    session.add(Tenant(id=1, name="tenant"))
    session.add(Task(
        id="task-1",
        tenant_id=1,
        name="remote",
        type="channel_view",
        status="running",
    ))
    action = Action(
        id="action-1",
        tenant_id=1,
        task_id="task-1",
        task_type="channel_view",
        action_type="noop_remote_probe",
        account_id=None,
        scheduled_at=observed_at - timedelta(minutes=1),
        executed_at=observed_at,
        status="unknown_after_send",
        payload={"gateway_request_identity": "request-1"},
        result={
            "success": False,
            "error_code": "content_contract_remote_reconcile_required",
        },
    )
    attempt = ExecutionAttempt(
        id="attempt-1",
        tenant_id=1,
        action_id=action.id,
        worker_id="dispatcher-1",
        account_id=None,
        attempt_no=1,
        status="result_unknown",
        before_call_at=observed_at - timedelta(seconds=2),
        gateway_call_started_at=observed_at - timedelta(seconds=1),
        after_call_at=observed_at,
        failure_type="unknown_after_send",
        result_snapshot={"gateway_request_identity": "request-1"},
    )
    session.add_all([action, attempt])
    session.flush()
    case = ensure_remote_reconcile_case(session, action, attempt)
    return action, attempt, case
