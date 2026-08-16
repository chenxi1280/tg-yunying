from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations/versions/0151_group_slot_pacing_due.py"
)
pytestmark = pytest.mark.no_postgres


def _migration_module():
    spec = importlib.util.spec_from_file_location("group_slot_pacing_due_0151", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_group_slot_pacing_due_migration_repairs_0150_shape_idempotently() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table(
        migration.TABLE_NAME,
        metadata,
        Column("id", String(36), primary_key=True),
        Column("release_not_before_at", String()),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns(migration.TABLE_NAME)
        }
        assert migration.COLUMN_NAME in columns

        migration.downgrade()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns(migration.TABLE_NAME)
        }
        assert migration.COLUMN_NAME not in columns
