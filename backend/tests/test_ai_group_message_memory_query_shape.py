from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AiGroupMessageMemory
from app.services._common import _now
from app.services.task_center.ai_message_memory import _window_memories


pytestmark = pytest.mark.no_postgres


def test_window_memories_projects_only_similarity_columns_across_groups() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    statements: list[str] = []

    def capture_select(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "from ai_group_message_memory" in statement.lower():
            statements.append(statement)

    now = _now()
    with Session(engine) as session:
        session.add(
            AiGroupMessageMemory(
                tenant_id=1,
                group_id=22,
                status="success",
                planned_at=now,
                normalized_text="跨群语义",
                raw_text="跨群语义原文",
                result={"large": "x" * 4096},
            )
        )
        session.commit()
        event.listen(engine, "before_cursor_execute", capture_select)
        try:
            rows = _window_memories(session, 1, 999, now - timedelta(days=7))
        finally:
            event.remove(engine, "before_cursor_execute", capture_select)

    assert len(rows) == 1
    assert rows[0].id
    assert rows[0].normalized_text == "跨群语义"
    assert rows[0].raw_text == "跨群语义原文"
    assert len(statements) == 1
    statement = statements[0].lower()
    select_clause = statement.split("from ai_group_message_memory", maxsplit=1)[0]
    assert [column.strip() for column in select_clause.removeprefix("select ").split(",")] == [
        "ai_group_message_memory.id",
        "ai_group_message_memory.normalized_text",
        "ai_group_message_memory.raw_text",
    ]
    assert "ai_group_message_memory.result" not in statement
    assert "ai_group_message_memory.group_id" not in statement


def test_window_memories_propagates_database_errors() -> None:
    class UnavailableSession:
        def execute(self, _statement):
            raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="^database unavailable$"):
        _window_memories(UnavailableSession(), 1, 999, _now() - timedelta(days=7))
