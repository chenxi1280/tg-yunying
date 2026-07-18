from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, JSON, MetaData, String, Table, create_engine, text


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0109_channel_planner_history_backpressure.py"


def test_channel_planner_history_migration_is_idempotent_and_reversible_on_sqlite() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    _action_metadata().create_all(engine)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        upgraded = _sqlite_index_names(connection)
        migration.downgrade()
        migration.downgrade()
        downgraded = _sqlite_index_names(connection)

    expected = {migration.MESSAGE_HISTORY_INDEX, migration.LEGACY_HISTORY_INDEX, migration.DAILY_CAPACITY_INDEX}
    assert expected.issubset(upgraded)
    assert not (expected & downgraded)


def _migration_module():
    spec = importlib.util.spec_from_file_location("channel_planner_history_backpressure_0109", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _action_metadata() -> MetaData:
    metadata = MetaData()
    Table(
        "actions",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("task_id", String(36)),
        Column("action_type", String(30)),
        Column("status", String(20)),
        Column("payload", JSON),
        Column("account_id", Integer),
    )
    return metadata


def _sqlite_index_names(connection) -> set[str]:
    return set(connection.execute(text(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND name NOT LIKE 'sqlite_autoindex_%'"
    )).scalars())
