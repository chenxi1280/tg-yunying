from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, ExecutionAttempt
from app.services.task_center import group_ai_scope
from app.services.task_center.group_ai_scope import successful_own_history_reply_facts


NOW = datetime(2026, 8, 2, 15, 30, 0)
WINDOW_DAYS = 7


@pytest.fixture(autouse=True)
def _deterministic_history_clock(monkeypatch):
    monkeypatch.setattr(group_ai_scope, "_now", lambda: NOW)
    monkeypatch.setattr(
        group_ai_scope,
        "get_settings",
        lambda: SimpleNamespace(ai_reply_target_history_window_days=WINDOW_DAYS),
    )


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


def test_own_history_query_excludes_targets_older_than_window() -> None:
    """近因窗口：窗口外的陈旧成功发送不得进入回复目标池（2026-08-17 生产事故：
    全历史扫描使 planner 事务分钟级持 task 行锁，发送坍塌）。"""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        stale = Action(
            id="stale-action",
            tenant_id=1,
            task_id="history-task",
            task_type="group_ai_chat",
            action_type="send_message",
            status="success",
            executed_at=NOW - timedelta(days=WINDOW_DAYS + 1),
            payload={"group_id": 7, "message_text": "陈旧消息"},
        )
        fresh = Action(
            id="fresh-action",
            tenant_id=1,
            task_id="history-task",
            task_type="group_ai_chat",
            action_type="send_message",
            status="success",
            executed_at=NOW - timedelta(days=1),
            payload={"group_id": 7, "message_text": "新鲜消息"},
        )
        session.add_all([stale, fresh])
        session.flush()
        session.add_all([
            ExecutionAttempt(
                action_id=stale.id, attempt_no=1, status="success", remote_message_id="7001",
            ),
            ExecutionAttempt(
                action_id=fresh.id, attempt_no=1, status="success", remote_message_id="7002",
            ),
        ])
        session.commit()

        rows = successful_own_history_reply_facts(
            session,
            tenant_id=1,
            task_id="history-task",
            group_id=7,
        )

    assert [(row.id, remote_id) for row, remote_id in rows] == [("fresh-action", "7002")]
