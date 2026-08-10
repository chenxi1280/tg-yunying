from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations/versions/0145_channel_view_daily_targets.py"
)
pytestmark = pytest.mark.no_postgres


def test_channel_view_daily_target_migration_is_idempotent() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    _create_parent_tables(engine)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        inspector = inspect(connection)
        columns = {
            column["name"]
            for column in inspector.get_columns("channel_view_daily_message_targets")
        }
        indexes = {
            index["name"]
            for index in inspector.get_indexes("channel_view_daily_message_targets")
        }

    assert {
        "lifetime_confirmed_at_attach",
        "ledger_confirmed_at_attach",
        "effective_target_snapshot",
        "active_until",
        "due_count",
    } <= columns
    assert indexes == {"ix_channel_view_daily_target_ledger"}


def _migration_module():
    spec = importlib.util.spec_from_file_location("channel_view_targets_0145", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_parent_tables(engine) -> None:
    metadata = MetaData()
    Table("tenants", metadata, Column("id", Integer, primary_key=True))
    Table("tasks", metadata, Column("id", String(36), primary_key=True))
    Table("task_day_ledgers", metadata, Column("id", String(36), primary_key=True))
    Table("channel_messages", metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)
