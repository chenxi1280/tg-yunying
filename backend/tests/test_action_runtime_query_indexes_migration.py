from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, DateTime, Integer, JSON, MetaData, String, Table, create_engine, text


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0096_action_runtime_query_indexes.py"


def test_action_runtime_indexes_are_idempotent_and_reversible_on_sqlite() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    _create_actions_table(engine)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        upgraded = _index_names(connection)
        migration.downgrade()
        migration.downgrade()
        downgraded = _index_names(connection)

    assert upgraded == set(migration.INDEX_NAMES)
    assert downgraded == set()


def test_action_runtime_indexes_use_concurrent_postgres_ddl(monkeypatch) -> None:
    migration = _migration_module()
    operation = _FakePostgresOp()
    migration.op = operation
    monkeypatch.setattr(migration, "_require_actions_table", lambda: None)
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: set())

    migration.upgrade()

    assert operation.statements == list(migration.POSTGRES_CREATE_STATEMENTS)


def _migration_module():
    spec = importlib.util.spec_from_file_location("action_runtime_query_indexes_0096", MIGRATION_PATH)
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
        Column("task_type", String(30)),
        Column("action_type", String(30)),
        Column("executed_at", DateTime),
        Column("payload", JSON),
        Column("result", JSON),
    )
    metadata.create_all(engine)


def _index_names(connection) -> set[str]:
    return set(connection.execute(text(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'actions' "
        "AND name NOT LIKE 'sqlite_autoindex_%'"
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
