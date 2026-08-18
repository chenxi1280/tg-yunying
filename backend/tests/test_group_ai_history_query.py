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

    history_queries = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT") and "execution_attempts" in statement
    ]
    query = history_queries[0]
    unused_query = history_queries[-1]
    assert result_rows == [("history-action", "9002")]
    assert unused_rows == []
    assert "GROUP BY execution_attempts.action_id" not in query
    assert "FROM execution_attempts JOIN" in query
    assert "action_id = actions.id" in query
    assert "row_number() OVER" in query
    assert "LEFT OUTER JOIN" in unused_query
    assert "SELECT DISTINCT" in unused_query
    assert "NOT (EXISTS" not in unused_query


def test_used_recent_targets_do_not_starve_later_candidate() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for ordinal in range(21):
            remote_id = str(8_000 + ordinal)
            session.add(Action(
                id=f"history-{ordinal}",
                tenant_id=1,
                task_id="history-task",
                task_type="group_ai_chat",
                action_type="send_message",
                status="success",
                executed_at=NOW - timedelta(minutes=ordinal),
                payload={"group_id": 7, "message_text": f"历史消息 {ordinal}"},
            ))
            session.add(ExecutionAttempt(
                action_id=f"history-{ordinal}",
                attempt_no=1,
                status="success",
                remote_message_id=remote_id,
            ))
            if ordinal < 20:
                session.add(Action(
                    id=f"pending-{ordinal}",
                    tenant_id=1,
                    task_id="other-task",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    status="pending",
                    payload={"group_id": 7, "reply_to_message_id": int(remote_id)},
                ))
        session.commit()

        rows = successful_own_history_reply_facts(
            session,
            tenant_id=1,
            task_id="history-task",
            group_id=7,
            exclude_used_statuses=("pending",),
            limit=1,
        )

    assert [(action.id, remote_id) for action, remote_id in rows] == [
        ("history-20", "8020"),
    ]


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
