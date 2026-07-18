from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


def test_hard_hourly_scheduler_checkpoint_migration_declares_column_and_wake_index() -> None:
    migrations = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    migration = migrations / "0105_hard_hourly_scheduler_checkpoint.py"

    assert migration.exists()

    source = migration.read_text()
    assert "hard_hourly_next_check_at" in source
    assert "ix_tasks_hard_hourly_wake" in source
