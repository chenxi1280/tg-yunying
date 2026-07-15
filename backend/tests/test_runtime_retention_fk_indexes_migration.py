from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0095_runtime_retention_fk_indexes.py"


def test_retention_fk_index_migration_is_idempotent_and_reversible_on_sqlite() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    _create_tables(engine, migration)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        upgraded = _index_names(connection, migration)
        migration.downgrade()
        migration.downgrade()
        downgraded = _index_names(connection, migration)

    expected = {name for name, _table, _column in migration.INDEX_DEFINITIONS}
    assert upgraded == expected
    assert downgraded == set()


def test_retention_fk_index_migration_uses_concurrent_postgres_ddl(monkeypatch) -> None:
    migration = _migration_module()
    operation = _FakePostgresOp()
    migration.op = operation
    monkeypatch.setattr(migration, "_require_tables", lambda: None)
    monkeypatch.setattr(migration, "_index_names", lambda *_args, **_kwargs: set())

    migration.upgrade()

    assert operation.statements == [
        f"CREATE INDEX CONCURRENTLY {name} ON {table} ({column})"
        for name, table, column in migration.INDEX_DEFINITIONS
    ]


def _migration_module():
    spec = importlib.util.spec_from_file_location("runtime_retention_fk_indexes_0095", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_tables(engine, migration) -> None:
    metadata = MetaData()
    for _name, table_name, column_name in migration.INDEX_DEFINITIONS:
        if table_name in metadata.tables:
            table = metadata.tables[table_name]
            if column_name not in table.c:
                table.append_column(Column(column_name, String(36)))
            continue
        Table(table_name, metadata, Column("id", String(36), primary_key=True), Column(column_name, String(36)))
    metadata.create_all(engine)


def _index_names(connection, migration) -> set[str]:
    return {
        item["name"]
        for _name, table, _column in migration.INDEX_DEFINITIONS
        for item in inspect(connection).get_indexes(table)
    }


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
