from copy import deepcopy

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, AuditLog, ExecutionAttempt
from app.services.task_center.telegram_worker_exit_reconcile import (
    apply_worker_exits, preview_worker_exits, verify_worker_exits,
)
from tests.test_engagement_upgrade_postgres import upgrade_database
from tests.test_telegram_worker_exit_reconcile import OPERATION, _legacy, _spec


@pytest.fixture
def original_call(upgrade_database):
    Base.metadata.create_all(upgrade_database)
    with Session(upgrade_database) as session:
        action, attempt = _legacy(session)
        session.commit()
        preview = preview_worker_exits(session, _spec(attempt))
        ids = action.id, attempt.id
    return upgrade_database, ids, preview


def test_lock_conflict_is_zero_write_then_idempotent_ack(original_call):
    database, ids, preview = original_call
    with Session(database) as owner, Session(database) as operator:
        owner.scalar(select(Action).where(Action.id == ids[0]).with_for_update())
        with pytest.raises(DBAPIError) as raised:
            apply_worker_exits(operator, preview, OPERATION)
        assert raised.value.orig.sqlstate == "55P03"
        operator.rollback()
        assert operator.get(ExecutionAttempt, ids[1]).result_snapshot["transport_termination_state"] == "unproven"
        owner.rollback()
        receipt = apply_worker_exits(operator, preview, OPERATION)
        operator.commit()
    with Session(database) as verification:
        assert verify_worker_exits(verification, receipt)["business_fields_preserved"]
        assert apply_worker_exits(verification, preview, OPERATION) == receipt
        assert len(list(verification.scalars(select(AuditLog)))) == 1


def test_attempt_drift_is_detected_after_cached_read(original_call):
    database, ids, preview = original_call
    with Session(database) as operator, Session(database) as writer:
        original = operator.get(ExecutionAttempt, ids[1])
        changed = writer.get(ExecutionAttempt, ids[1])
        changed.result_snapshot = {**changed.result_snapshot, "late_original_evidence": "arrived"}
        writer.commit()
        assert "late_original_evidence" not in original.result_snapshot
        with pytest.raises(ValueError, match="preview_conflict"):
            apply_worker_exits(operator, preview, OPERATION)
        operator.rollback()
        assert original.result_snapshot["transport_termination_state"] == "unproven"
        assert list(operator.scalars(select(AuditLog))) == []


def test_commit_rollback_does_not_leave_ack_or_audit(original_call):
    database, ids, preview = original_call
    with Session(database) as operator:
        receipt = apply_worker_exits(operator, deepcopy(preview), OPERATION)
        assert receipt["attempts"]
        operator.rollback()
    with Session(database) as verification:
        attempt = verification.get(ExecutionAttempt, ids[1])
        assert attempt.status == "result_unknown"
        assert attempt.result_snapshot["transport_termination_state"] == "unproven"
        assert list(verification.scalars(select(AuditLog))) == []


def test_utc_database_session_preserves_original_call_instant(original_call):
    database, ids, preview = original_call
    with Session(database) as operator:
        operator.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        attempt = operator.get(ExecutionAttempt, ids[1])
        utc_preview = preview_worker_exits(operator, _spec(attempt))
        assert utc_preview["state_hash"] == preview["state_hash"]
        assert utc_preview["state"]["attempts"][0]["call_at"] == "2026-09-05T01:00:00+00:00"
        receipt = apply_worker_exits(operator, preview, OPERATION)
        operator.commit()
    with Session(database) as verification:
        assert verify_worker_exits(verification, receipt)["business_fields_preserved"]
