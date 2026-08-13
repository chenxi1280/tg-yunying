from datetime import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, ExecutionAttempt
from app.services.task_center.group_ai_scope import successful_own_history_reply_facts


NOW = datetime(2026, 8, 2, 15, 30, 0)


pytestmark = pytest.mark.no_postgres


def test_own_history_query_is_correlated_to_the_task_actions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    statements: list[str] = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _parameters, _context, _many: statements.append(statement),
    )

    with Session(engine) as session:
        action = Action(
            id="history-action",
            tenant_id=1,
            task_id="history-task",
            task_type="group_ai_chat",
            action_type="send_message",
            status="success",
            executed_at=NOW,
            payload={"group_id": 7, "message_text": "历史消息"},
        )
        session.add(action)
        session.flush()
        session.add_all([
            ExecutionAttempt(
                action_id=action.id,
                attempt_no=1,
                status="success",
                remote_message_id="9001",
            ),
            ExecutionAttempt(
                action_id=action.id,
                attempt_no=2,
                status="success",
                remote_message_id="9002",
            ),
        ])
        session.commit()

        rows = successful_own_history_reply_facts(
            session,
            tenant_id=1,
            task_id="history-task",
            group_id=7,
        )
        session.add(Action(
            id="pending-reply",
            tenant_id=1,
            task_id="history-task",
            task_type="group_ai_chat",
            action_type="send_message",
            status="pending",
            payload={"group_id": 7, "message_text": "回复", "reply_to_message_id": 9002},
        ))
        session.commit()
        unused_rows = successful_own_history_reply_facts(
            session,
            tenant_id=1,
            task_id="history-task",
            group_id=7,
            exclude_used_statuses=("pending",),
        )
        result_rows = [(row.id, remote_id) for row, remote_id in rows]

    query = next(
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT") and "execution_attempts" in statement
    )
    assert result_rows == [("history-action", "9002")]
    assert unused_rows == []
    assert "GROUP BY execution_attempts.action_id" not in query
    assert "FROM execution_attempts JOIN" in query
    assert "action_id = actions.id" in query
    assert "row_number() OVER" in query
