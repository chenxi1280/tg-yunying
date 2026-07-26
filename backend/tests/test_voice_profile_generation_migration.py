from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "versions" / "0124_voice_profile_generation_jobs.py"


def test_voice_profile_generation_migration_is_idempotent_and_reversible_on_sqlite() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:", future=True)
    _parent_tables().create_all(engine)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        upgraded_tables = set(inspect(connection).get_table_names())
        migration.downgrade()
        migration.downgrade()
        downgraded_tables = set(inspect(connection).get_table_names())

    assert set(migration.GENERATION_TABLES) <= upgraded_tables
    assert not (set(migration.GENERATION_TABLES) & downgraded_tables)


def _migration_module():
    spec = importlib.util.spec_from_file_location("voice_profile_generation_0124", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("voice profile generation migration could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parent_tables() -> MetaData:
    metadata = MetaData()
    Table("tenants", metadata, Column("id", Integer, primary_key=True))
    Table("tg_accounts", metadata, Column("id", Integer, primary_key=True))
    return metadata
