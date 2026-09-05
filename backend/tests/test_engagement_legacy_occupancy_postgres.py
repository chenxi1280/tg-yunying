from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text

from app.database import SessionLocal
from app.models import (AccountPool, Action, ExecutionAttempt, GatewayRequestEvidenceJournal,
    Task, Tenant, TgAccount)
from app.services.task_center.engagement_legacy_occupancy import (
    LegacyOccupancyScope, read_legacy_attempt_occupancy)


TENANT_ID = 951_101
POOL_ID = 951_102
ACCOUNT_ID = 951_103
BEIJING_MIDNIGHT = datetime(2026, 9, 4, 16, tzinfo=timezone.utc)


def _seed(session, call_at):
    session.add(Tenant(id=TENANT_ID, name="存量占用测试"))
    session.flush()
    session.add(AccountPool(id=POOL_ID, tenant_id=TENANT_ID, name="普通组"))
    session.flush()
    session.add(TgAccount(id=ACCOUNT_ID, tenant_id=TENANT_ID, pool_id=POOL_ID,
        display_name="测试账号", phone_masked="test"))
    task = Task(tenant_id=TENANT_ID, type="channel_like", name="原日期测试")
    session.add(task)
    session.flush()
    action = Action(tenant_id=TENANT_ID, task_id=task.id, task_type=task.type,
        action_type="like_message", account_id=ACCOUNT_ID, status="closed_unknown",
        pacing_due_at=BEIJING_MIDNIGHT - timedelta(seconds=1))
    session.add(action)
    session.flush()
    attempt = ExecutionAttempt(tenant_id=TENANT_ID, action_id=action.id, account_id=ACCOUNT_ID,
        status="result_unknown", gateway_call_started_at=call_at,
        result_snapshot={"transport_termination_state": "acknowledged"})
    session.add(attempt)
    session.flush()
    return action, attempt


@pytest.mark.parametrize("offset,expected", [(-1, 0), (0, 1), (86400, 0)])
def test_postgres_filters_actual_beijing_day_without_rewriting_original_day(offset, expected):
    with SessionLocal() as session:
        session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        action, attempt = _seed(session, BEIJING_MIDNIGHT + timedelta(seconds=offset))
        for suffix in ("original", "deadline"):
            session.add(GatewayRequestEvidenceJournal(tenant_id=TENANT_ID, action_id=action.id,
                execution_attempt_id=attempt.id, account_id=ACCOUNT_ID,
                gateway_request_identity=f"{attempt.id}-{suffix}",
                request_fingerprint="r" * 64, target_fingerprint="t" * 64,
                result_fingerprint="s" * 64, evidence_hash="e" * 64,
                remote_mutation_state="unknown"))
        session.flush()
        statements = []

        def record(_connection, _cursor, statement, _params, _context, _many):
            statements.append(statement.split()[0].upper())

        connection = session.connection()
        event.listen(connection, "before_cursor_execute", record)
        try:
            rows = read_legacy_attempt_occupancy(session, LegacyOccupancyScope(
                tenant_id=TENANT_ID, account_ids=(ACCOUNT_ID,), task_day=date(2026, 9, 5)))
        finally:
            event.remove(connection, "before_cursor_execute", record)
        assert len(rows) == expected
        assert statements == ["SELECT"]
        if rows:
            assert rows[0].original_task_day == date(2026, 9, 4)
            assert rows[0].call_day == date(2026, 9, 5)
            assert not rows[0].remote_inflight and not rows[0].issues
        assert attempt.status == "result_unknown" and action.status == "closed_unknown"
        session.rollback()


def test_postgres_migrated_account_index_is_valid_and_contains_unknown_calls():
    with SessionLocal() as session:
        valid, definition = session.execute(text("""
            SELECT i.indisvalid, pg_get_indexdef(i.indexrelid)
            FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = :name AND n.nspname = current_schema()
        """), {"name": "ix_execution_attempts_account_usage"}).one()
        assert valid
        assert "tenant_id, account_id, gateway_call_started_at" in definition
        assert "result_unknown" in definition and "gateway_call_started_at IS NOT NULL" in definition
