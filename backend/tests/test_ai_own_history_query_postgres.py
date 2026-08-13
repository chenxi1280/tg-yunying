from __future__ import annotations

from datetime import timedelta
from time import perf_counter

import pytest
from sqlalchemy import delete, event, text

from app.database import Base, SessionLocal, engine
from app.models import Action, ExecutionAttempt, Task, Tenant
from app.services._common import _now
from app.services.task_center.group_ai_scope import successful_own_history_reply_facts


TENANT_ID = 913_813
TASK_ID = "pg-ai-own-history-query"
GROUP_ID = 913_814
HISTORY_COUNT = 5_000
LATEST_REMOTE_ID = str(8_000_000 + HISTORY_COUNT - 1)
MAX_QUERY_SECONDS = 2.0


@pytest.mark.allow_missing_rule_binding
def test_postgres_own_history_queries_are_scoped_and_remote_indexed() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    statements: list[str] = []

    def track_select(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "execution_attempts" in statement.lower() and "from actions" in statement.lower():
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", track_select)
    try:
        _seed_history()
        with SessionLocal() as session:
            started = perf_counter()
            candidates = successful_own_history_reply_facts(
                session,
                tenant_id=TENANT_ID,
                task_id=TASK_ID,
                group_id=GROUP_ID,
                limit=20,
            )
            exact = successful_own_history_reply_facts(
                session,
                tenant_id=TENANT_ID,
                task_id=TASK_ID,
                group_id=GROUP_ID,
                remote_message_id=LATEST_REMOTE_ID,
                limit=1,
            )
            elapsed = perf_counter() - started
            exact_plan = _exact_query_plan(session)
    finally:
        event.remove(engine, "before_cursor_execute", track_select)
        _cleanup()

    assert len(candidates) == 20
    assert len(exact) == 1
    assert exact[0][1] == LATEST_REMOTE_ID
    assert elapsed < MAX_QUERY_SECONDS
    assert len(statements) == 2
    assert "row_number() over" in statements[0].lower()
    assert "select execution_attempts.remote_message_id" not in statements[0].lower()
    assert "ix_execution_attempts_success_remote" in exact_plan


def _seed_history() -> None:
    now_value = _now()
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="AI own history query postgres"))
        session.flush()
        session.add(Task(
            id=TASK_ID,
            tenant_id=TENANT_ID,
            name="AI own history query postgres",
            type="group_ai_chat",
            status="running",
        ))
        session.flush()
        for start in range(0, HISTORY_COUNT, 500):
            actions = []
            attempts = []
            for index in range(start, min(start + 500, HISTORY_COUNT)):
                action_id = f"pg-ai-own-{index:05d}"
                actions.append(Action(
                    id=action_id,
                    tenant_id=TENANT_ID,
                    task_id=TASK_ID,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="success",
                    payload={"group_id": GROUP_ID, "message_text": f"history-{index}"},
                    executed_at=now_value + timedelta(seconds=index),
                ))
                attempts.append(ExecutionAttempt(
                    tenant_id=TENANT_ID,
                    action_id=action_id,
                    attempt_no=1,
                    status="success",
                    remote_message_id=str(8_000_000 + index),
                ))
            session.add_all(actions)
            session.add_all(attempts)
            session.commit()


def _exact_query_plan(session) -> str:
    sql = """
        EXPLAIN (COSTS OFF)
        SELECT execution_attempts.action_id
        FROM execution_attempts
        WHERE execution_attempts.status = 'success'
          AND execution_attempts.remote_message_id = :remote_message_id
        LIMIT 1
    """
    return "\n".join(session.execute(
        text(sql),
        {"remote_message_id": LATEST_REMOTE_ID},
    ).scalars())


def _cleanup() -> None:
    with SessionLocal() as session:
        session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.action_id.like("pg-ai-own-%")))
        session.execute(delete(Action).where(Action.tenant_id == TENANT_ID))
        session.execute(delete(Task).where(Task.tenant_id == TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()
