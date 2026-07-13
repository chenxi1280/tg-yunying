from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, event, inspect
from sqlalchemy.exc import NoSuchTableError
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


class _RecordingPostgresOp:
    def __init__(self, events: list[str]):
        self.events = events
        self.bind = type("Bind", (), {"dialect": type("Dialect", (), {"name": "postgresql"})()})()

    def get_bind(self):
        return self.bind

    def get_context(self):
        events = self.events

        class RecordingContext:
            @contextmanager
            def autocommit_block(self):
                events.append("enter_autocommit")
                try:
                    yield
                finally:
                    events.append("exit_autocommit")

        return RecordingContext()

    def execute(self, statement):
        self.events.append(str(statement))


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


def test_message_memory_index_upgrade_fails_when_target_table_is_missing() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    migration = _migration_module()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(NoSuchTableError):
            migration.upgrade()


def test_postgres_concurrent_ddl_runs_inside_autocommit_block(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration_module()
    upgrade_events: list[str] = []
    monkeypatch.setattr(migration, "op", _RecordingPostgresOp(upgrade_events))
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: set())

    migration.upgrade()

    assert upgrade_events == [
        "enter_autocommit",
        f"CREATE INDEX CONCURRENTLY {INDEX_NAME} ON ai_group_message_memory "
        "(tenant_id, status, planned_at DESC)",
        "exit_autocommit",
    ]

    downgrade_events: list[str] = []
    monkeypatch.setattr(migration, "op", _RecordingPostgresOp(downgrade_events))
    monkeypatch.setattr(migration, "_has_table", lambda: True)
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: {INDEX_NAME})

    migration.downgrade()

    assert downgrade_events == [
        "enter_autocommit",
        f"DROP INDEX CONCURRENTLY {INDEX_NAME}",
        "exit_autocommit",
    ]


def test_postgres_index_catalog_distinguishes_valid_and_invalid_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration_module()
    calls: list[tuple[str, dict[str, str]]] = []

    class FakeResult:
        def __init__(self, values: tuple[str, ...]):
            self.values = values

        def scalars(self):
            return self.values

    class FakeBind:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        @staticmethod
        def execute(statement, parameters):
            sql = str(statement)
            calls.append((sql, parameters))
            if "index_meta.indisvalid" in sql:
                return FakeResult(("valid_index",))
            return FakeResult(("valid_index", INDEX_NAME))

    fake_op = type("FakeOp", (), {"get_bind": staticmethod(lambda: FakeBind())})()
    monkeypatch.setattr(migration, "op", fake_op)

    valid_names = migration._index_names()
    all_names = migration._index_names(valid_only=False)

    assert valid_names == {"valid_index"}
    assert all_names == {"valid_index", INDEX_NAME}
    assert calls[0][1] == {"table_name": migration.TABLE_NAME}
    assert "index_meta.indisvalid" in calls[0][0]
    assert calls[1][1] == {"table_name": migration.TABLE_NAME}
    assert "index_meta.indisvalid" not in calls[1][0]


def test_postgres_upgrade_skips_only_a_valid_existing_index(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration_module()
    events: list[str] = []
    monkeypatch.setattr(migration, "op", _RecordingPostgresOp(events))
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: {INDEX_NAME})

    migration.upgrade()

    assert events == []


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
