from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, inspect


MIGRATION_PATH = Path(__file__).resolve().parents[2] / "backend/migrations/versions/0100_ai_memory_covering_index.py"
pytestmark = pytest.mark.no_postgres


def test_ai_memory_covering_index_is_idempotent_and_reversible_on_sqlite() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    _create_actions_table(engine)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        upgraded = {item["name"] for item in inspect(connection).get_indexes("actions")}
        migration.downgrade()
        migration.downgrade()
        downgraded = {item["name"] for item in inspect(connection).get_indexes("actions")}

    assert upgraded == {migration.INDEX_NAME}
    assert downgraded == set()


def test_ai_memory_covering_index_uses_concurrent_include_postgres_ddl(monkeypatch) -> None:
    migration = _migration_module()
    operation = _FakePostgresOp()
    migration.op = operation
    monkeypatch.setattr(migration, "_require_actions_table", lambda: None)
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: set())

    migration.upgrade()

    assert operation.statements == [migration.POSTGRES_CREATE]
    assert "CONCURRENTLY" in migration.POSTGRES_CREATE
    assert "INCLUDE (executed_at, scheduled_at)" in migration.POSTGRES_CREATE
    assert "status IN ('success', 'unknown_after_send')" in migration.POSTGRES_CREATE


def _migration_module():
    spec = importlib.util.spec_from_file_location("ai_memory_covering_index_0100", MIGRATION_PATH)
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
        Column("created_at", DateTime),
        Column("executed_at", DateTime),
        Column("scheduled_at", DateTime),
    )
    metadata.create_all(engine)


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
