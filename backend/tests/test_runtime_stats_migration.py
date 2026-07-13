from __future__ import annotations

from contextlib import contextmanager
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Date, DateTime, Integer, MetaData, String, Table, create_engine, inspect


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0093_runtime_stats_indexes.py"


def _migration_module():
    spec = importlib.util.spec_from_file_location("runtime_stats_indexes_0093", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_metadata() -> MetaData:
    metadata = MetaData()
    Table("tenants", metadata, Column("id", Integer, primary_key=True))
    Table("tasks", metadata, Column("id", String(36), primary_key=True))
    Table(
        "actions",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("task_id", String(36)),
        Column("status", String(20)),
        Column("action_type", String(30)),
        Column("account_id", Integer),
        Column("scheduled_at", DateTime),
        Column("executed_at", DateTime),
        Column("lease_expires_at", DateTime),
    )
    Table(
        "task_account_daily_coverage",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("task_id", String(36)),
        Column("coverage_date", Date),
        Column("state", String(40)),
        Column("targeted_at", DateTime),
        Column("account_id", Integer),
    )
    return metadata


def test_runtime_stats_migration_upgrades_and_downgrades_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    _legacy_metadata().create_all(engine)
    migration = _migration_module()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        upgraded_tables = set(inspect(connection).get_table_names())
        action_indexes = {item["name"] for item in inspect(connection).get_indexes("actions")}
        coverage_indexes = {item["name"] for item in inspect(connection).get_indexes("task_account_daily_coverage")}
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
        downgraded_tables = set(inspect(connection).get_table_names())

    assert migration.CURSOR_TABLE in upgraded_tables
    assert {"ix_actions_task_stats_reconcile", "ix_actions_executing_recovery"}.issubset(action_indexes)
    assert "ix_task_daily_coverage_plan_ready" in coverage_indexes
    assert migration.CURSOR_TABLE not in downgraded_tables


@pytest.mark.parametrize("operation_name", ["upgrade", "downgrade"])
def test_runtime_stats_migration_rejects_missing_required_tables(operation_name: str) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    migration = _migration_module()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match="^required tables missing:"):
            getattr(migration, operation_name)()


def test_runtime_stats_downgrade_rejects_missing_cursor_table() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    _legacy_metadata().create_all(engine)
    migration = _migration_module()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match=f"^required table missing: {migration.CURSOR_TABLE}$"):
            migration.downgrade()


@pytest.mark.parametrize("message", ["same-name invalid index", "postgres index build failed"])
def test_runtime_stats_postgres_index_failure_propagates(monkeypatch, message: str) -> None:
    migration = _migration_module()
    failure = RuntimeError(message)

    class FakeBind:
        dialect = type("Dialect", (), {"name": "postgresql"})()

    class FakeContext:
        @contextmanager
        def autocommit_block(self):
            yield

    class FakeOp:
        @staticmethod
        def get_bind():
            return FakeBind()

        @staticmethod
        def get_context():
            return FakeContext()

        @staticmethod
        def execute(_statement):
            raise failure

    monkeypatch.setattr(migration, "op", FakeOp())
    monkeypatch.setattr(migration, "_index_names", lambda *_args, **_kwargs: set())

    with pytest.raises(RuntimeError) as exc_info:
        migration._create_index("ix_invalid", "actions", "scheduled_at, id", " WHERE status = 'executing'")

    assert exc_info.value is failure
