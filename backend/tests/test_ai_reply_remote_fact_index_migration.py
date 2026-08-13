from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text


MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend/migrations/versions/0146_ai_reply_remote_fact_index.py"
)
pytestmark = pytest.mark.no_postgres


def test_ai_reply_remote_fact_index_is_idempotent_and_reversible_on_sqlite() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    _create_table(engine)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        upgraded = set(connection.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=:table_name"
        ), {"table_name": migration.TABLE_NAME}).scalars())
        migration.downgrade()
        migration.downgrade()
        downgraded = set(connection.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=:table_name"
        ), {"table_name": migration.TABLE_NAME}).scalars())

    assert migration.INDEX_NAME in upgraded
    assert migration.INDEX_NAME not in downgraded


def test_ai_reply_remote_fact_index_matches_exact_guard_contract() -> None:
    migration = _migration_module()

    assert "CONCURRENTLY IF NOT EXISTS" in migration.POSTGRES_CREATE
    assert migration.INDEX_COLUMNS == "remote_message_id, action_id, attempt_no DESC"
    assert migration.PREDICATE == "status = 'success' AND remote_message_id <> ''"


def _migration_module():
    spec = importlib.util.spec_from_file_location("ai_reply_remote_index_0146", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_table(engine) -> None:
    metadata = MetaData()
    Table(
        "execution_attempts",
        metadata,
        Column("id", String, primary_key=True),
        Column("action_id", String),
        Column("attempt_no", Integer),
        Column("status", String),
        Column("remote_message_id", String),
    )
    metadata.create_all(engine)
