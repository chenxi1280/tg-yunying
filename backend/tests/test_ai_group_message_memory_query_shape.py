from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, event, inspect
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AiGroupMessageMemory
from app.services._common import _now
from app.services.task_center.ai_message_memory import _window_memories


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0091_ai_message_memory_dedup_index.py"
INDEX_NAME = "ix_ai_group_message_memory_tenant_status_planned"


def _migration_module():
    assert MIGRATION_PATH.exists(), f"missing migration: {MIGRATION_PATH.name}"
    spec = importlib.util.spec_from_file_location("ai_message_memory_dedup_index_0091", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_message_memory_model_declares_tenant_window_index_in_query_order() -> None:
    index = next(
        item for item in AiGroupMessageMemory.__table__.indexes
        if item.name == INDEX_NAME
    )

    assert tuple(str(expression) for expression in index.expressions) == (
        "ai_group_message_memory.tenant_id",
        "ai_group_message_memory.status",
        "planned_at DESC",
    )


def test_message_memory_index_migration_declares_concurrent_postgres_contract() -> None:
    migration = _migration_module()
    source = MIGRATION_PATH.read_text()

    assert migration.revision == "0091_ai_memory_index"
    assert migration.down_revision == "0090_ai_group_fallback"
    assert "autocommit_block" in source
    assert "CREATE INDEX CONCURRENTLY" in source
    assert "DROP INDEX CONCURRENTLY" in source
    assert INDEX_NAME in source


def test_message_memory_index_migration_upgrades_and_downgrades_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata = MetaData()
    Table(
        "ai_group_message_memory",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("tenant_id", Integer, nullable=False),
        Column("status", String(40), nullable=False),
        Column("planned_at", DateTime, nullable=False),
    )
    metadata.create_all(engine)
    migration = _migration_module()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        upgraded_indexes = {item["name"] for item in inspect(connection).get_indexes("ai_group_message_memory")}
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
        downgraded_indexes = {item["name"] for item in inspect(connection).get_indexes("ai_group_message_memory")}

    assert INDEX_NAME in upgraded_indexes
    assert INDEX_NAME not in downgraded_indexes


def test_postgres_index_ddl_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration_module()
    failure = RuntimeError("postgres index build failed")

    class FakeBind:
        dialect = type("Dialect", (), {"name": "postgresql"})()

    class FakeContext:
        @contextmanager
        def autocommit_block(self):
            yield

    class FakeOp:
        @staticmethod
        def get_bind():
            return FakeBind()

        @staticmethod
        def get_context():
            return FakeContext()

        @staticmethod
        def execute(_statement):
            raise failure

    monkeypatch.setattr(migration, "op", FakeOp())
    monkeypatch.setattr(migration, "_has_table", lambda: True)
    monkeypatch.setattr(migration, "_index_names", lambda: set())

    with pytest.raises(RuntimeError) as exc_info:
        migration.upgrade()

    assert exc_info.value is failure


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
