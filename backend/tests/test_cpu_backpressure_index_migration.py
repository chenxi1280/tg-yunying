from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


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
