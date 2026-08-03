from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, create_engine, text


pytestmark = pytest.mark.no_postgres
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend/migrations/versions/0136_ai_generation_group_occupancy_index.py"
)


def test_group_occupancy_index_is_idempotent_and_reversible_on_sqlite() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    _create_actions_table(engine)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        upgraded = _sqlite_index_names(connection)
        migration.downgrade()
        migration.downgrade()
        downgraded = _sqlite_index_names(connection)

    assert upgraded == {migration.INDEX_NAME}
    assert downgraded == set()


def test_group_occupancy_index_matches_runtime_predicate(monkeypatch) -> None:
    migration = _migration_module()
    operation = _FakePostgresOp()
    migration.op = operation
    monkeypatch.setattr(migration, "_require_actions_table", lambda: None)
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: set())

    migration.upgrade()

    [statement] = operation.statements
    assert "CREATE INDEX CONCURRENTLY" in statement
    assert "tenant_id" in statement
    assert "group_id" in statement
    assert "ai_generation_status" in statement
    assert "generating" in statement
    assert "ready" in statement
    assert "message_text" in statement


def _migration_module():
    spec = importlib.util.spec_from_file_location("ai_generation_group_occupancy_0136", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_actions_table(engine) -> None:
    metadata = MetaData()
    Table(
        "actions",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("tenant_id", Integer),
        Column("task_type", String(30)),
        Column("action_type", String(30)),
        Column("status", String(20)),
        Column("payload", JSON),
    )
    metadata.create_all(engine)


def _sqlite_index_names(connection) -> set[str]:
    return set(connection.execute(text(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name = 'actions' "
        "AND name NOT LIKE 'sqlite_autoindex_%'",
    )).scalars())


class _FakeDialect:
    name = "postgresql"


class _FakeBind:
    dialect = _FakeDialect()


class _FakeContext:
    def autocommit_block(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


class _FakePostgresOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def get_bind(self):
        return _FakeBind()

    def get_context(self):
        return _FakeContext()

    def execute(self, statement: str) -> None:
        self.statements.append(statement)
