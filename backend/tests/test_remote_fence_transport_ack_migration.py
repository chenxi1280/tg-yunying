from __future__ import annotations

from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine


pytestmark = pytest.mark.no_postgres


def test_transport_ack_migration_does_not_invent_termination_for_open_fence() -> None:
    migration = import_module(
        "migrations.versions.0212_remote_fence_transport_ack"
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE remote_invocation_fences ("
            "id TEXT PRIMARY KEY, state TEXT, transport_terminated_at DATETIME)"
        )
        connection.exec_driver_sql(
            "INSERT INTO remote_invocation_fences VALUES "
            "('open', 'remote_unknown', NULL), "
            "('closed', 'terminal', '2026-09-05 00:00:00')"
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        rows = connection.exec_driver_sql(
            "SELECT id, runner_generation, transport_termination_state "
            "FROM remote_invocation_fences ORDER BY id"
        ).all()

    assert rows == [
        ("closed", 1, "legacy_terminal"),
        ("open", 1, "unproven"),
    ]
