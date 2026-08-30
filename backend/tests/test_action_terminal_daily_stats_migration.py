from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


pytestmark = pytest.mark.no_postgres
MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0174_action_terminal_daily_stats.py"
)


def test_action_terminal_daily_stats_migration_is_reversible() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        inspector = inspect(connection)
        indexes = {item["name"] for item in inspector.get_indexes(migration.TABLE)}
        migration.downgrade()
        tables_after_downgrade = set(inspect(connection).get_table_names())

    assert "ix_action_terminal_daily_stats_lookup" in indexes
    assert migration.TABLE not in tables_after_downgrade


def _migration_module():
    spec = importlib.util.spec_from_file_location("action_terminal_stats_0174", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
