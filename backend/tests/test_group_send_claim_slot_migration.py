from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


pytestmark = pytest.mark.no_postgres

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "migrations/versions/0127_group_send_claim_slot.py"


def test_group_send_claim_slot_migration_is_idempotent_and_reversible_on_sqlite() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tg_groups (id INTEGER PRIMARY KEY)"))
        migration.op = Operations(MigrationContext.configure(connection))

        migration.upgrade()
        migration.upgrade()
        assert migration.COLUMN in {column["name"] for column in inspect(connection).get_columns(migration.TABLE)}

        migration.downgrade()
        migration.downgrade()
        assert migration.COLUMN not in {column["name"] for column in inspect(connection).get_columns(migration.TABLE)}


def _migration_module():
    spec = importlib.util.spec_from_file_location("group_send_claim_slot_migration", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
