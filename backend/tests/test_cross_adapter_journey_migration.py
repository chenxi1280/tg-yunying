from __future__ import annotations

from datetime import date, datetime, timezone
from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import MetaData, Table, create_engine


pytestmark = pytest.mark.no_postgres


def test_journey_migration_allows_rejected_successor_beside_active_plan() -> None:
    migration = import_module(
        "migrations.versions.0210_cross_adapter_journey"
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        _create_fixture(connection)
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        table = Table(
            "cross_adapter_source_journey_plan_revisions",
            MetaData(),
            autoload_with=connection,
        )
        connection.execute(table.insert(), [
            _plan_values("active", 1),
            _plan_values("rejected", 2),
        ])
        rows = connection.exec_driver_sql(
            "SELECT plan_revision, state FROM "
            "cross_adapter_source_journey_plan_revisions "
            "ORDER BY plan_revision"
        ).all()

    assert rows == [(1, "active"), (2, "rejected")]


def _plan_values(state: str, revision: int) -> dict:
    return {
        "id": f"plan-{revision}",
        "tenant_id": 1,
        "source_revision_id": "source-1",
        "task_day": date(2026, 9, 5),
        "plan_revision": revision,
        "source_task_set_hash": "tasks",
        "policy_revision": "policy",
        "adapter_constraints": [],
        "hard_constraint_hash": "hard",
        "objective_policy": {},
        "edge_set": [],
        "edge_set_hash": "edges",
        "overlap_metrics": {},
        "deficits": [],
        "decision": "feasible" if state == "active" else "unachievable",
        "input_hash": f"input-{revision}",
        "supersedes_plan_id": "plan-1" if revision > 1 else None,
        "state": state,
        "created_at": datetime.now(timezone.utc),
    }


def _create_fixture(connection) -> None:
    for statement in (
        "CREATE TABLE tenants (id INTEGER PRIMARY KEY)",
        "CREATE TABLE tasks (id TEXT PRIMARY KEY)",
        "CREATE TABLE tg_accounts (id INTEGER PRIMARY KEY)",
        "CREATE TABLE channel_message_source_revisions (id TEXT PRIMARY KEY)",
        "INSERT INTO tenants VALUES (1)",
        "INSERT INTO channel_message_source_revisions VALUES ('source-1')",
    ):
        connection.exec_driver_sql(statement)
