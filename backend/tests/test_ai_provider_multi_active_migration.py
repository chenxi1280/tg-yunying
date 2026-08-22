from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Boolean, Column, Integer, MetaData, Table, create_engine, inspect, text


pytestmark = pytest.mark.no_postgres
VERSIONS = Path(__file__).resolve().parents[1] / "migrations/versions"


def test_multi_active_migration_drops_single_active_index_and_adds_flag() -> None:
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table(
        "ai_providers",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("is_active", Boolean, nullable=False),
    )
    Table(
        "tenant_ai_settings",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Integer, nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE UNIQUE INDEX uq_ai_provider_single_active "
            "ON ai_providers (is_active) WHERE is_active = 1"
        ))
        migration = _migration()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

    inspector = inspect(engine)
    indexes = {item["name"] for item in inspector.get_indexes("ai_providers")}
    columns = {item["name"] for item in inspector.get_columns("tenant_ai_settings")}
    assert "uq_ai_provider_single_active" not in indexes
    assert "ai_provider_route_fallback_enabled" in columns


def test_multi_active_migration_accepts_fresh_schema_with_flag_already_present() -> None:
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table(
        "ai_providers",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("is_active", Boolean, nullable=False),
    )
    Table(
        "tenant_ai_settings",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Integer, nullable=False),
        Column("ai_provider_route_fallback_enabled", Boolean, nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        migration = _migration()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

    columns = {item["name"] for item in inspect(engine).get_columns("tenant_ai_settings")}
    assert "ai_provider_route_fallback_enabled" in columns


def _migration():
    path = VERSIONS / "0161_ai_provider_multi_active_failover.py"
    spec = importlib.util.spec_from_file_location("provider_failover_0161", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration_load_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
