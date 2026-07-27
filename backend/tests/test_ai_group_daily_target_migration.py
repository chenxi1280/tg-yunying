from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


pytestmark = pytest.mark.no_postgres
MIGRATION_PATH = Path(__file__).resolve().parents[1] / "migrations/versions/0128_ai_group_daily_targets.py"


def test_daily_target_migration_accepts_current_model_bootstrap_and_repeats() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_parent_tables(connection)
        migration.op = Operations(MigrationContext.configure(connection))

        migration.upgrade()
        migration.upgrade()

        assert migration._has_table("task_group_daily_targets")
        assert migration._has_index(
            "task_group_daily_targets",
            "ix_task_group_daily_target_task_date",
        )


def _create_parent_tables(connection) -> None:
    connection.execute(text("CREATE TABLE tenants (id INTEGER PRIMARY KEY)"))
    connection.execute(text("CREATE TABLE tasks (id VARCHAR(36) PRIMARY KEY)"))
    connection.execute(text("CREATE TABLE tg_groups (id INTEGER PRIMARY KEY)"))


def _migration_module():
    spec = importlib.util.spec_from_file_location("ai_group_daily_target_migration", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
