from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from scripts.migrate_ai_group_daily_targets import migrated_open_payload, normalized_task_config


pytestmark = pytest.mark.no_postgres
MIGRATION_PATH = Path(__file__).resolve().parents[1] / "migrations/versions/0128_ai_group_daily_targets.py"


def test_legacy_per_account_two_migrates_to_frozen_account_count() -> None:
    config = normalized_task_config(
        {
            "per_account_daily_min_messages": 2,
            "per_account_daily_max_messages": 2,
            "hard_hourly_target_enabled": True,
            "hourly_min_messages": 10,
        },
        frozen_account_count=7,
    )

    assert config["daily_message_target"] == 7
    assert config["account_coverage_mode"] == "all_accounts_daily"
    assert "per_account_daily_min_messages" not in config
    assert "hard_hourly_target_enabled" not in config


def test_explicit_daily_target_is_preserved_above_account_floor() -> None:
    config = normalized_task_config({"daily_message_target": 12}, frozen_account_count=7)

    assert config["daily_message_target"] == 12


def test_coverage_bound_hard_action_is_rebound_without_hour_bucket() -> None:
    payload, decision = migrated_open_payload(
        {
            "coverage_ledger_id": "coverage-1",
            "hard_hourly_target": True,
            "hard_hourly_bucket": "2026-07-28T10:00:00",
            "hard_hourly_goal_at_plan": 10,
        },
        coverage_bound=True,
    )

    assert decision == "rebound_daily_target"
    assert "hard_hourly_target" not in payload
    assert "hard_hourly_bucket" not in payload


def test_unbound_hard_action_is_not_reused_for_daily_volume() -> None:
    payload, decision = migrated_open_payload(
        {"hard_hourly_target": True, "hard_hourly_bucket": "2026-07-28T10:00:00"},
        coverage_bound=False,
    )

    assert decision == "skip_unbound_hard_target"
    assert payload["hard_hourly_target"] is True


def test_daily_target_migration_accepts_current_model_bootstrap_and_repeats() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_parent_tables(connection)
        migration.op = Operations(MigrationContext.configure(connection))

        migration.upgrade()
        migration.upgrade()

        assert migration._has_table("task_group_daily_targets")
        assert migration._has_index(
            "task_group_daily_targets",
            "ix_task_group_daily_target_task_date",
        )


def _create_parent_tables(connection) -> None:
    connection.execute(text("CREATE TABLE tenants (id INTEGER PRIMARY KEY)"))
    connection.execute(text("CREATE TABLE tasks (id VARCHAR(36) PRIMARY KEY)"))
    connection.execute(text("CREATE TABLE tg_groups (id INTEGER PRIMARY KEY)"))


def _migration_module():
    spec = importlib.util.spec_from_file_location("ai_group_daily_target_migration", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
