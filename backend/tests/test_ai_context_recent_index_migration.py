from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, Table, Text, create_engine, text


MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend/migrations/versions/0142_ai_context_recent_index.py"
)
pytestmark = pytest.mark.no_postgres


def test_ai_context_recent_index_is_idempotent_and_reversible_on_sqlite() -> None:
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

    assert upgraded == {migration.INDEX_NAME}
    assert downgraded == set()


def test_ai_context_recent_index_matches_query_contract() -> None:
    migration = _migration_module()

    assert "CONCURRENTLY IF NOT EXISTS" in migration.POSTGRES_CREATE
    assert "coalesce(sent_at, created_at) DESC" in migration.POSTGRES_CREATE
    assert "id DESC" in migration.POSTGRES_CREATE
    assert "is_bot IS false AND content <> ''" in migration.POSTGRES_CREATE


def _migration_module():
    spec = importlib.util.spec_from_file_location("ai_context_recent_index_0142", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_table(engine) -> None:
    metadata = MetaData()
    Table(
        "group_context_messages",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Integer),
        Column("group_id", Integer),
        Column("is_bot", Boolean),
        Column("content", Text),
        Column("sent_at", DateTime),
        Column("created_at", DateTime),
    )
    metadata.create_all(engine)
