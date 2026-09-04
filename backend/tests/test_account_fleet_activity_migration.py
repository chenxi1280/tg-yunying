from __future__ import annotations

from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


pytestmark = pytest.mark.no_postgres


def test_fleet_migration_backfills_policy_and_projection_state() -> None:
    migration = import_module(
        "migrations.versions.0211_account_fleet_activity"
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        _create_fixture(connection)
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        policy = connection.exec_driver_sql(
            "SELECT tenant_id, account_pool_id, rolling_window_days, state "
            "FROM account_fleet_activity_policy_revisions"
        ).one()
        projection = connection.exec_driver_sql(
            "SELECT fact_id, projection_kind, state "
            "FROM fulfillment_fact_projection_states"
        ).one()

    assert policy == (1, 10, 3, "active")
    assert projection == ("fact-1", "fleet_activity", "pending")
    indexes = inspect(engine).get_indexes(
        "account_fleet_activity_policy_revisions"
    )
    active_index = next(
        item for item in indexes
        if item["name"] == "uq_fleet_activity_policy_active"
    )
    assert active_index["unique"] == 1


def _create_fixture(connection) -> None:
    statements = (
        "CREATE TABLE tenants (id INTEGER PRIMARY KEY)",
        "CREATE TABLE tg_accounts (id INTEGER PRIMARY KEY)",
        "CREATE TABLE account_pools (id INTEGER PRIMARY KEY, tenant_id INTEGER, pool_purpose TEXT)",
        "CREATE TABLE fulfillment_remote_facts (fact_id TEXT PRIMARY KEY, fact_kind TEXT)",
        "CREATE TABLE fulfillment_fact_projection_states (id TEXT PRIMARY KEY, fact_id TEXT, projection_kind TEXT, expected_target_version INTEGER, state TEXT, next_retry_at DATETIME, updated_at DATETIME)",
        "INSERT INTO tenants VALUES (1)",
        "INSERT INTO account_pools VALUES (10, 1, 'normal'), (20, 1, 'auxiliary')",
        "INSERT INTO fulfillment_remote_facts VALUES ('fact-1', 'view_observed'), ('fact-2', 'unsupported')",
    )
    for statement in statements:
        connection.exec_driver_sql(statement)
