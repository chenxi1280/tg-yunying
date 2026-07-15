from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, JSON, MetaData, String, Table, create_engine, inspect


MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend/migrations/versions/0098_action_generation_failure_index.py"
)


def test_generation_failure_index_is_idempotent_and_reversible_on_sqlite() -> None:
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


def test_generation_failure_index_uses_concurrent_postgres_ddl(monkeypatch) -> None:
    migration = _migration_module()
    operation = _FakePostgresOp()
    migration.op = operation
    monkeypatch.setattr(migration, "_require_actions_table", lambda: None)
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: set())

    migration.upgrade()

    assert operation.statements == [migration.POSTGRES_CREATE]
    assert "ai_generation_status" in migration.POSTGRES_CREATE
    assert "NOT IN" in migration.POSTGRES_CREATE


def _migration_module():
    spec = importlib.util.spec_from_file_location("action_generation_failure_index_0098", MIGRATION_PATH)
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
        Column("task_id", String(36)),
        Column("action_type", String(30)),
        Column("payload", JSON),
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
