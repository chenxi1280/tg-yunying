from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect


MIGRATION_PATH = Path(__file__).resolve().parents[1] / "migrations/versions/0133_group_listener_cursor.py"
pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("precreate_columns", [False, True])
def test_group_listener_cursor_migration_is_idempotent(precreate_columns: bool) -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    _create_group_table(engine, precreate_columns=precreate_columns)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        columns = {column["name"] for column in inspect(connection).get_columns("tg_groups")}

    assert {"listener_remote_cursor", "listener_cursor_status"} <= columns


def _migration_module():
    spec = importlib.util.spec_from_file_location("group_listener_cursor_0133", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_group_table(engine, *, precreate_columns: bool) -> None:
    columns = [Column("id", Integer, primary_key=True)]
    if precreate_columns:
        columns.extend(
            [
                Column("listener_remote_cursor", String(160), nullable=False, default=""),
                Column("listener_cursor_status", String(20), nullable=False, default="unproven"),
            ]
        )
    metadata = MetaData()
    Table("tg_groups", metadata, *columns)
    metadata.create_all(engine)
