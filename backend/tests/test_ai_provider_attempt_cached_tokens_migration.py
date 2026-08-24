from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect


pytestmark = pytest.mark.no_postgres
VERSIONS = Path(__file__).resolve().parents[1] / "migrations/versions"


def test_cached_tokens_migration_is_additive_and_idempotent() -> None:
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table(
        "ai_provider_attempts",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("prompt_tokens", Integer, nullable=False),
        Column("completion_tokens", Integer, nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        migration = _migration()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()

    columns = {
        item["name"]: item
        for item in inspect(engine).get_columns("ai_provider_attempts")
    }
    assert columns["cached_tokens"]["nullable"] is False


def _migration():
    path = VERSIONS / "0166_ai_provider_attempt_cached_tokens.py"
    spec = importlib.util.spec_from_file_location("ai_attempt_cache_0166", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration_load_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
