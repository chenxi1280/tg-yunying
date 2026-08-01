from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import pytest

from app.database import Base
from app.models import (
    Action,
    ExecutionAttempt,
    GatewayRequestEvidenceJournal,
    Task,
    Tenant,
    TgAccount,
)
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.gateway_evidence_journal import (
    GatewayResultEvidence,
    bind_gateway_request_identity,
    record_gateway_result_evidence,
)
from app.services.task_center.remote_reconciliation import (
    apply_remote_reconcile_evidence,
    ensure_remote_reconcile_case,
    evidence_from_gateway_journal,
)


pytestmark = pytest.mark.no_postgres


def test_gateway_identity_and_result_journal_replay_are_durable_facts() -> None:
    engine = _engine()
    with Session(engine) as session:
        action, attempt = _seed_started_attempt(session)
        identity = bind_gateway_request_identity(action, attempt)
        evidence = GatewayResultEvidence(
            remote_message_id="remote-1",
            remote_mutation_started=True,
        )
        first = record_gateway_result_evidence(session, action, attempt, evidence)
        second = record_gateway_result_evidence(session, action, attempt, evidence)

        assert identity.startswith("telegram-gateway:")
        assert first.id == second.id
        assert first.state == "recorded"
        assert session.scalar(select(func.count(
            GatewayRequestEvidenceJournal.id,
        ))) == 1


def test_different_result_for_same_request_identity_is_quarantined() -> None:
    engine = _engine()
    with Session(engine) as session:
        action, attempt = _seed_started_attempt(session)
        bind_gateway_request_identity(action, attempt)
        row = record_gateway_result_evidence(
            session,
            action,
            attempt,
            GatewayResultEvidence(remote_message_id="remote-1"),
        )
        record_gateway_result_evidence(
            session,
            action,
            attempt,
            GatewayResultEvidence(remote_message_id="remote-2"),
        )
        assert row.state == "conflict"
        assert row.remote_message_id == "remote-1"


def test_same_identity_with_payload_drift_is_quarantined() -> None:
    engine = _engine()
    with Session(engine) as session:
        action, attempt = _seed_started_attempt(session)
        bind_gateway_request_identity(action, attempt)
        evidence = GatewayResultEvidence(
            remote_fact_id="700",
            remote_mutation_started=True,
        )
        row = record_gateway_result_evidence(
            session, action, attempt, evidence,
        )
        action.payload = {
            **action.payload,
            "message_id": 700,
            "reaction_emoji": "🔥",
        }
        record_gateway_result_evidence(
            session, action, attempt, evidence,
        )

        assert row.state == "conflict"


def test_remote_case_consumes_journal_without_resending() -> None:
    engine = _engine()
    with Session(engine) as session:
        action, attempt = _seed_started_attempt(session)
        bind_gateway_request_identity(action, attempt)
        record_gateway_result_evidence(
            session,
            action,
            attempt,
            GatewayResultEvidence(
                failure_code="rpc_rejected_before_mutation",
                remote_mutation_started=False,
            ),
        )
        action.status = "unknown_after_send"
        attempt.status = "result_unknown"
        case = ensure_remote_reconcile_case(session, action, attempt)

        evidence = evidence_from_gateway_journal(session, case.id)
        outcome = apply_remote_reconcile_evidence(
            session,
            case.id,
            evidence,
            actor="release-owner",
        )

        assert evidence.result == "remote_absence_proven"
        assert outcome.state == "remote_absence_proven"
        assert action.status == "failed"


def test_dispatcher_b0_identity_and_pre_b1_result_journal_are_persisted() -> None:
    engine = _engine()
    with Session(engine) as session:
        action, _ = _seed_started_attempt(session)
        session.query(ExecutionAttempt).delete()
        account = session.get(TgAccount, 11)
        attempt = dispatcher._begin_execution_attempt(session, action, account)
        dispatcher._mark_gateway_call_started(session, attempt, commit=False)
        session.commit()

        session.refresh(action)
        session.refresh(attempt)
        assert action.result["gateway_request_identity"]
        assert action.result["gateway_request_fingerprint"]
        assert action.result["gateway_target_fingerprint"]
        assert (
            attempt.result_snapshot["gateway_request_identity"]
            == action.result["gateway_request_identity"]
        )

        action.status = "success"
        action.result = {**action.result, "success": True}
        dispatcher._finish_execution_attempt(
            attempt,
            action,
            remote_id="remote-after-b0",
            remote_mutation_started=True,
        )
        session.flush()
        journal = session.query(GatewayRequestEvidenceJournal).one()
        assert journal.remote_message_id == "remote-after-b0"
        assert journal.remote_mutation_state == "true"


def test_operation_result_persists_source_remote_fact_identity() -> None:
    engine = _engine()
    with Session(engine) as session:
        action, attempt = _seed_started_attempt(session)
        account = session.get(TgAccount, 11)
        bind_gateway_request_identity(action, attempt)

        dispatcher._apply_operation_result(
            action,
            account,
            True,
            attempt=attempt,
            remote_fact_id="700",
            remote_mutation_started=True,
        )

        journal = session.query(GatewayRequestEvidenceJournal).one()
        assert action.result["remote_fact_id"] == "700"
        assert attempt.result_snapshot["remote_fact_id"] == "700"
        assert journal.remote_fact_id == "700"
        assert journal.remote_mutation_state == "true"


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed_started_attempt(session: Session):
    observed_at = _now()
    session.add(Tenant(id=1, name="tenant"))
    session.add(Task(
        id="task-1",
        tenant_id=1,
        name="journal",
        type="group_ai_chat",
        status="running",
    ))
    session.add(TgAccount(
        id=11,
        tenant_id=1,
        display_name="account",
        phone_masked="***11",
        status="在线",
        session_ciphertext="session",
    ))
    action = Action(
        id="action-1",
        tenant_id=1,
        task_id="task-1",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        scheduled_at=observed_at - timedelta(minutes=1),
        status="executing",
        payload={
            "group_id": 8,
            "chat_id": "-1008",
            "message_text": "body",
        },
        result={},
    )
    attempt = ExecutionAttempt(
        id="attempt-1",
        tenant_id=1,
        action_id=action.id,
        account_id=11,
        attempt_no=1,
        status="gateway_call_started",
        before_call_at=observed_at - timedelta(seconds=1),
        gateway_call_started_at=observed_at,
        result_snapshot={},
    )
    session.add_all([action, attempt])
    session.flush()
    return action, attempt
