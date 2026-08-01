from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

import pytest


pytestmark = pytest.mark.no_postgres
MIGRATION_PATH = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0134_shared_dispatch_recovery.py"


def test_shared_dispatch_migration_is_idempotent_on_legacy_schema() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        _create_legacy_dispatch_tables(connection)
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()

    inspector = inspect(engine)
    assert set(migration.NEW_TABLES) <= set(inspector.get_table_names())
    assert set(migration.MODEL_COLUMNS["dispatch_claim_scopes"]) <= _columns(
        inspector, "dispatch_claim_scopes",
    )
    assert "effective_unclaimed_count" in _columns(
        inspector, "dispatch_claim_windows",
    )
    assert "dispatch_contract_version" in _columns(
        inspector, "dispatch_claim_shard_allocations",
    )


def _migration_module():
    spec = importlib.util.spec_from_file_location("migration_0134", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_legacy_dispatch_tables(connection) -> None:
    connection.execute(text(
        "CREATE TABLE dispatch_claim_scopes ("
        "id VARCHAR(36) PRIMARY KEY, dispatcher_scope VARCHAR(80), "
        "claim_capacity INTEGER, active_claim_count INTEGER, "
        "opportunity_cursor INTEGER, version INTEGER, "
        "created_at DATETIME, updated_at DATETIME)"
    ))
    connection.execute(text(
        "CREATE TABLE dispatch_claim_windows ("
        "id VARCHAR(36) PRIMARY KEY, dispatcher_scope VARCHAR(80), "
        "bucket_start DATETIME, bucket_end DATETIME, claim_capacity INTEGER, "
        "active_claim_count INTEGER, unclaimed_allocated_count INTEGER, "
        "allocation_epoch INTEGER, allocation_state VARCHAR(24), "
        "rebuild_input_hash VARCHAR(64), pending_rebuild_release_count INTEGER, "
        "allocation_scope_version INTEGER, allocation_scope_active_count INTEGER, "
        "rebuild_input_version INTEGER, ready_rebuild_snapshot_hash VARCHAR(64), "
        "version INTEGER, created_at DATETIME, updated_at DATETIME)"
    ))
    connection.execute(text(
        "CREATE TABLE dispatch_claim_shard_allocations ("
        "id VARCHAR(36) PRIMARY KEY, dispatch_claim_window_id VARCHAR(36), "
        "dispatch_allocation_epoch INTEGER, rebuild_input_hash VARCHAR(64), "
        "dispatch_rebuild_snapshot_hash VARCHAR(64), account_shard_total INTEGER, "
        "account_shard_index INTEGER, required_claims INTEGER, "
        "active_claim_count INTEGER, unclaimed_allocated_count INTEGER, "
        "reason VARCHAR(120), version INTEGER, created_at DATETIME, updated_at DATETIME)"
    ))


def _columns(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}
