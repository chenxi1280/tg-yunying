from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, DateTime, JSON, MetaData, String, Table, create_engine, text


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0108_runtime_retention_backpressure.py"


def test_runtime_retention_backpressure_migration_declares_expression_indexes() -> None:
    assert MIGRATION_PATH.exists(), "missing runtime retention backpressure migration"

    source = MIGRATION_PATH.read_text()
    for expected in (
        "ix_actions_runtime_detail_retention",
        "COALESCE(executed_at, scheduled_at, created_at)",
        "created_at, id",
        "ix_runtime_cleanup_audits_kind_created_at",
        "CAST(summary ->> 'cleanup_kind' AS varchar)",
    ):
        assert expected in source


def test_runtime_retention_backpressure_migration_is_idempotent_and_reversible_on_sqlite() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    _legacy_metadata().create_all(engine)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        upgraded = _sqlite_index_names(connection)
        migration.downgrade()
        migration.downgrade()
        downgraded = _sqlite_index_names(connection)

    assert upgraded == {migration.ACTION_INDEX, migration.AUDIT_INDEX}
    assert downgraded == set()


def test_runtime_retention_batch_orders_by_indexed_age() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "task_center"
        / "runtime_retention.py"
    ).read_text()

    assert ".order_by(age.asc(), Action.created_at.asc(), Action.id.asc())" in source


def _migration_module():
    spec = importlib.util.spec_from_file_location("runtime_retention_backpressure_0108", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_metadata() -> MetaData:
    metadata = MetaData()
    Table(
        "actions",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("executed_at", DateTime),
        Column("scheduled_at", DateTime),
        Column("created_at", DateTime),
    )
    Table(
        "runtime_cleanup_audits",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("summary", JSON),
        Column("created_at", DateTime),
    )
    return metadata


def _sqlite_index_names(connection) -> set[str]:
    return set(connection.execute(text(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND name NOT LIKE 'sqlite_autoindex_%'"
    )).scalars())
