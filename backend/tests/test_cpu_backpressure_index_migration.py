from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, JSON, MetaData, String, Table, create_engine, inspect


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "versions" / "0111_metrics_summary_anchor_index.py"


def test_cpu_backpressure_index_migration_declares_hot_path_indexes() -> None:
    migrations = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    migration = migrations / "0106_hard_hourly_recovery_backpressure.py"

    assert migration.exists()

    source = migration.read_text()
    for index_name in (
        "ix_actions_recovery_hard_hourly_pending",
        "ix_actions_pending_membership_fast_track",
    ):
        assert index_name in source


def test_metrics_summary_anchor_index_migration_declares_hot_path_index() -> None:
    assert MIGRATION_PATH.exists()

    source = MIGRATION_PATH.read_text()
    assert "ix_actions_task_voice_anchor_fact" in source
    assert "voice_profile_anchor_rewritten" in source


def test_metrics_summary_anchor_index_migration_is_idempotent_and_reversible_on_sqlite() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    _actions_metadata().create_all(engine)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        upgraded_indexes = {item["name"] for item in inspect(connection).get_indexes("actions")}
        migration.downgrade()
        migration.downgrade()
        downgraded_indexes = {item["name"] for item in inspect(connection).get_indexes("actions")}

    assert migration.INDEX_NAME in upgraded_indexes
    assert migration.INDEX_NAME not in downgraded_indexes


def _migration_module():
    spec = importlib.util.spec_from_file_location("metrics_summary_anchor_index_0111", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _actions_metadata() -> MetaData:
    metadata = MetaData()
    Table(
        "actions",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("tenant_id", Integer),
        Column("task_id", String(36)),
        Column("action_type", String(30)),
        Column("result", JSON),
    )
    return metadata
