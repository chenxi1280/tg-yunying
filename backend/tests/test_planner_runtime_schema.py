from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.models import SourcePacingState, WorkerRuntimeResourceSample
from app.services.task_center.planner_resource_sampler import (
    read_cgroup_memory,
    read_smaps_rollup,
)


pytestmark = pytest.mark.no_postgres
MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations/versions/0152_planner_pacing_runtime.py"
)
NEW_TABLES = {
    "task_planner_wake_states",
    "task_admission_projections",
    "task_runtime_active_blockers",
    "task_source_subscriptions",
    "source_pacing_states",
    "source_pacing_admissions",
    "worker_runtime_resource_samples",
}


def _migration_module():
    spec = importlib.util.spec_from_file_location("planner_pacing_runtime_0152", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_prerequisites(connection) -> None:
    statements = (
        "CREATE TABLE tenants (id INTEGER PRIMARY KEY)",
        "CREATE TABLE tasks (id VARCHAR(36) PRIMARY KEY)",
        "CREATE TABLE actions (id VARCHAR(36) PRIMARY KEY)",
        "CREATE TABLE execution_attempts (id VARCHAR(36) PRIMARY KEY)",
        "CREATE TABLE listener_source_state (id VARCHAR(36) PRIMARY KEY, "
        "source_type VARCHAR(40))",
        "CREATE TABLE task_runtime_summary (id VARCHAR(36) PRIMARY KEY)",
        "CREATE TABLE task_membership_admission_items (id INTEGER PRIMARY KEY, "
        "task_id VARCHAR(36))",
    )
    for statement in statements:
        connection.execute(text(statement))
    for table_name in (
        "task_group_daily_message_slots",
        "comment_fulfillment_obligations",
        "reaction_fulfillment_obligations",
        "view_fulfillment_obligations",
    ):
        connection.execute(text(
            f"CREATE TABLE {table_name} "
            "(id VARCHAR(36) PRIMARY KEY, tenant_id INTEGER, "
            "release_not_before_at DATETIME)"
        ))


def _columns(connection, table_name: str) -> set[str]:
    return {
        str(column["name"])
        for column in inspect(connection).get_columns(table_name)
    }


def _indexes(connection, table_name: str) -> set[str]:
    return {
        str(index["name"])
        for index in inspect(connection).get_indexes(table_name)
    }


def test_source_pacing_state_is_shared_across_tasks_by_schema() -> None:
    constraints = SourcePacingState.__table__.constraints
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }

    assert ("tenant_id", "pacing_domain", "source_key_hash") in unique_columns
    assert all("task_id" not in columns for columns in unique_columns)


def test_resource_sample_uses_bigint_for_cgroup_byte_counters() -> None:
    for column_name in (
        "cgroup_current_bytes",
        "cgroup_peak_bytes",
        "cgroup_limit_bytes",
        "cgroup_event_count",
    ):
        column = WorkerRuntimeResourceSample.__table__.columns[column_name]
        assert column.type.__class__.__name__ == "BigInteger"


def test_smaps_rollup_parser_records_private_memory(tmp_path) -> None:
    path = tmp_path / "smaps_rollup"
    path.write_text(
        "Rss: 1000 kB\nPss: 900 kB\nPrivate_Dirty: 800 kB\n"
        "Anonymous: 700 kB\nAnonHugePages: 512 kB\n",
        encoding="ascii",
    )

    assert read_smaps_rollup(path) == {
        "rss_kib": 1000,
        "pss_kib": 900,
        "private_dirty_kib": 800,
        "anonymous_kib": 700,
        "anon_huge_pages_kib": 512,
    }


def test_cgroup_sampler_auto_detects_v1_and_v2(tmp_path) -> None:
    v1 = tmp_path / "v1"
    memory = v1 / "memory"
    memory.mkdir(parents=True)
    for name, value in {
        "memory.usage_in_bytes": "101",
        "memory.max_usage_in_bytes": "202",
        "memory.limit_in_bytes": "303",
        "memory.failcnt": "4",
    }.items():
        (memory / name).write_text(value, encoding="ascii")

    assert read_cgroup_memory(v1).version == 1
    assert read_cgroup_memory(v1).peak_bytes == 202

    v2 = tmp_path / "v2"
    v2.mkdir()
    (v2 / "cgroup.controllers").write_text("memory", encoding="ascii")
    (v2 / "memory.current").write_text("404", encoding="ascii")
    (v2 / "memory.peak").write_text("505", encoding="ascii")
    (v2 / "memory.max").write_text("max", encoding="ascii")
    (v2 / "memory.events").write_text("oom 2\noom_kill 1\n", encoding="ascii")

    sample = read_cgroup_memory(v2)
    assert sample.version == 2
    assert sample.current_bytes == 404
    assert sample.limit_bytes == 0
    assert sample.event_count == 3


def test_0152_upgrade_is_idempotent_and_downgrade_removes_contract() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    migration = _migration_module()

    with engine.begin() as connection:
        _create_prerequisites(connection)
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()

        assert NEW_TABLES.issubset(set(inspect(connection).get_table_names()))
        assert {"lifecycle_epoch", "blocker_revision"}.issubset(
            _columns(connection, "task_runtime_summary")
        )
        assert {
            "eligibility_rank", "eligibility_revision", "planner_last_selected_at"
        }.issubset(_columns(connection, "task_membership_admission_items"))
        assert "ix_membership_admission_planner_selection" in _indexes(
            connection, "task_membership_admission_items"
        )
        assert "ix_view_fulfillment_obligations_source_cursor" in _indexes(
            connection, "view_fulfillment_obligations"
        )

        migration.downgrade()
        assert NEW_TABLES.isdisjoint(set(inspect(connection).get_table_names()))
        assert "lifecycle_epoch" not in _columns(connection, "task_runtime_summary")
