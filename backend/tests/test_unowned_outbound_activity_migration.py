from __future__ import annotations

from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


pytestmark = pytest.mark.no_postgres


def test_unowned_activity_migration_backfills_scoped_policy() -> None:
    migration = import_module(
        "migrations.versions.0213_unowned_outbound_activity"
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE tenants (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE tg_accounts (id INTEGER PRIMARY KEY)"
        )
        connection.exec_driver_sql("INSERT INTO tenants VALUES (1)")
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        policy = connection.exec_driver_sql(
            "SELECT tenant_id, revision, hold_seconds_by_class, state "
            "FROM external_account_use_policy_revisions"
        ).one()

    assert policy[0:2] == (1, 1)
    assert '"authored_message": 600' in policy[2]
    assert policy[3] == "active"
    assert {
        "external_account_use_policy_revisions",
        "unowned_outbound_activity_observations",
        "account_external_use_holds",
    }.issubset(set(inspect(engine).get_table_names()))
