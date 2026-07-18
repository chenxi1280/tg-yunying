from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


def test_cpu_backpressure_index_migration_declares_hot_path_indexes() -> None:
    migrations = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    migration = migrations / "0104_cpu_backpressure_indexes.py"

    assert migration.exists()

    source = migration.read_text()
    for index_name in (
        "ix_actions_planner_open_normal_global",
        "ix_actions_planner_open_normal_task",
        "ix_actions_planner_open_hard_hourly_task",
        "ix_worker_heartbeats_last_seen_at",
        "ix_runtime_metric_snapshots_metric_dimension_captured",
    ):
        assert index_name in source
