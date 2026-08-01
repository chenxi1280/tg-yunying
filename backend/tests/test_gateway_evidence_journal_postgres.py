from __future__ import annotations

from sqlalchemy import delete

from app.database import SessionLocal
from app.models import (
    Action,
    ExecutionAttempt,
    GatewayRequestEvidenceJournal,
    Task,
    Tenant,
    TgAccount,
)
from app.services._common import _now
from app.services.task_center.gateway_evidence_journal import (
    GatewayResultEvidence,
    bind_gateway_request_identity,
    persist_gateway_result_evidence,
)


TENANT_ID = 991_340
TASK_ID = "pg-gateway-journal-task"
ACTION_ID = "pg-gateway-journal-action"
ATTEMPT_ID = "pg-gateway-journal-attempt"
ACCOUNT_ID = 991_341


def test_postgres_gateway_journal_survives_b1_rollback() -> None:
    _cleanup()
    try:
        _seed_committed_b0()
        with SessionLocal() as session:
            action = session.get(Action, ACTION_ID)
            attempt = session.get(ExecutionAttempt, ATTEMPT_ID)
            action.status = "success"
            attempt.status = "success"
            persisted = persist_gateway_result_evidence(
                action,
                attempt,
                GatewayResultEvidence(
                    remote_message_id="remote-pg-1",
                    remote_mutation_started=True,
                ),
            )
            assert persisted is True
            session.rollback()

        with SessionLocal() as session:
            action = session.get(Action, ACTION_ID)
            journal = session.query(GatewayRequestEvidenceJournal).filter_by(
                action_id=ACTION_ID,
                execution_attempt_id=ATTEMPT_ID,
            ).one()
            assert action.status == "executing"
            assert journal.remote_message_id == "remote-pg-1"
            assert journal.remote_mutation_state == "true"
    finally:
        _cleanup()


def _seed_committed_b0() -> None:
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="gateway journal postgres"))
        session.commit()
        session.add(Task(
            id=TASK_ID,
            tenant_id=TENANT_ID,
            name="gateway journal postgres",
            type="channel_view",
            status="running",
        ))
        session.add(TgAccount(
            id=ACCOUNT_ID,
            tenant_id=TENANT_ID,
            display_name="journal account",
            phone_masked="***341",
            status="在线",
        ))
        session.flush()
        action = Action(
            id=ACTION_ID,
            tenant_id=TENANT_ID,
            task_id=TASK_ID,
            task_type="channel_view",
            action_type="view_message",
            account_id=ACCOUNT_ID,
            scheduled_at=_now(),
            status="executing",
            payload={"channel_id": 88, "remote_message_id": "source-1"},
            result={},
        )
        attempt = ExecutionAttempt(
            id=ATTEMPT_ID,
            tenant_id=TENANT_ID,
            action_id=ACTION_ID,
            account_id=ACCOUNT_ID,
            attempt_no=1,
            status="gateway_call_started",
            before_call_at=_now(),
            gateway_call_started_at=_now(),
            result_snapshot={},
        )
        session.add_all([action, attempt])
        session.flush()
        bind_gateway_request_identity(action, attempt)
        session.commit()


def _cleanup() -> None:
    with SessionLocal() as session:
        session.execute(delete(GatewayRequestEvidenceJournal).where(
            GatewayRequestEvidenceJournal.action_id == ACTION_ID,
        ))
        session.execute(delete(ExecutionAttempt).where(
            ExecutionAttempt.id == ATTEMPT_ID,
        ))
        session.execute(delete(Action).where(Action.id == ACTION_ID))
        session.execute(delete(Task).where(Task.id == TASK_ID))
        session.execute(delete(TgAccount).where(TgAccount.id == ACCOUNT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()
