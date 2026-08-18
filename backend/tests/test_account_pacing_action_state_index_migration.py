from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect


pytestmark = pytest.mark.no_postgres
MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations/versions/0154_account_pacing_action_state_index.py"
)


def test_account_pacing_action_state_index_is_idempotent_on_sqlite() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    _metadata().create_all(engine)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        upgraded = _index_names(connection, migration.INDEX_SPECS)
        migration.downgrade()
        migration.downgrade()
        downgraded = _index_names(connection, migration.INDEX_SPECS)

    assert upgraded == {spec[0] for spec in migration.INDEX_SPECS}
    assert downgraded == set()


def test_account_pacing_action_state_index_uses_concurrent_postgres_ddl() -> None:
    source = MIGRATION_PATH.read_text()

    assert "CREATE INDEX CONCURRENTLY" in source
    assert "DROP INDEX CONCURRENTLY" in source
    assert "action_id, state" in source
    assert "source_pacing_state_id, state, call_not_before_at" in source


def _migration_module():
    spec = importlib.util.spec_from_file_location("account_pacing_index_0154", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _metadata() -> MetaData:
    metadata = MetaData()
    Table(
        "account_pacing_reservations",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("action_id", String(36)),
        Column("state", String(24)),
    )
    Table(
        "source_pacing_admissions",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("source_pacing_state_id", String(36)),
        Column("state", String(32)),
        Column("call_not_before_at", String(40)),
    )
    return metadata


def _index_names(connection, specs) -> set[str]:
    return {
        str(index["name"])
        for _name, table_name, _columns in specs
        for index in inspect(connection).get_indexes(table_name)
    }
