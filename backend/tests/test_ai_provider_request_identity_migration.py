from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect


pytestmark = pytest.mark.no_postgres
MIGRATION = Path(__file__).resolve().parents[1] / "migrations/versions/0182_ai_provider_request_identity.py"


def test_request_identity_migration_is_idempotent() -> None:
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table(
        "ai_provider_attempts", metadata,
        Column("id", String(36), primary_key=True),
        Column("request_hash", String(64), nullable=False),
    )
    metadata.create_all(engine)
    migration = _migration()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
    columns = {item["name"]: item for item in inspect(engine).get_columns("ai_provider_attempts")}
    assert columns["provider_request_id"]["nullable"] is False
    assert columns["provider_request_id"]["type"].length == 200


def _migration():
    spec = importlib.util.spec_from_file_location("ai_provider_request_identity_0182", MIGRATION)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration_load_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
