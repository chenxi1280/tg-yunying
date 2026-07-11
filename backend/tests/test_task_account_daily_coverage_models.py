from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0088_ai_group_daily_coverage.py"


def _models():
    from app.models import AccountEligibilityEvent, TaskAccountDailyCoverage

    return AccountEligibilityEvent, TaskAccountDailyCoverage


def _migration_module():
    spec = importlib.util.spec_from_file_location("ai_group_daily_coverage_0088", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_daily_coverage_model_declares_auditable_completion_fields() -> None:
    _, coverage_model = _models()
    columns = set(coverage_model.__table__.c.keys())

    assert {
        "id", "tenant_id", "task_id", "group_id", "account_id", "membership_item_id",
        "coverage_date", "target_count", "confirmed_count", "state", "reserved_action_id",
        "last_success_action_id", "last_remote_message_id", "blocker_code", "blocker_detail",
        "next_eligible_at", "targeted_at", "completed_at", "created_at", "updated_at",
    } == columns


def test_daily_coverage_model_prevents_duplicate_account_obligations() -> None:
    _, coverage_model = _models()
    table = coverage_model.__table__
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    indexes = {index.name: tuple(column.name for column in index.columns) for index in table.indexes}

    assert ("tenant_id", "task_id", "group_id", "account_id", "coverage_date") in unique_columns
    assert indexes["ix_task_daily_coverage_task_date_state"] == (
        "task_id", "coverage_date", "state", "next_eligible_at",
    )
    assert indexes["ix_task_daily_coverage_account_date"] == ("tenant_id", "account_id", "coverage_date")


def test_account_eligibility_event_has_unprocessed_queue_index() -> None:
    event_model, _ = _models()
    indexes = {index.name: tuple(column.name for column in index.columns) for index in event_model.__table__.indexes}

    assert indexes["ix_account_eligibility_events_pending"] == ("processed_at", "next_attempt_at", "occurred_at")
    assert indexes["ix_account_eligibility_events_account"] == ("tenant_id", "account_id", "occurred_at")


def _legacy_metadata() -> MetaData:
    metadata = MetaData()
    Table("tenants", metadata, Column("id", Integer, primary_key=True))
    Table("tasks", metadata, Column("id", String(36), primary_key=True))
    Table("tg_groups", metadata, Column("id", Integer, primary_key=True))
    Table("tg_accounts", metadata, Column("id", Integer, primary_key=True))
    Table("actions", metadata, Column("id", String(36), primary_key=True))
    Table("task_membership_admission_items", metadata, Column("id", Integer, primary_key=True))
    return metadata


def test_coverage_migration_upgrades_and_downgrades_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    _legacy_metadata().create_all(engine)
    migration = _migration_module()

    assert migration.down_revision == "0087_rank_deboost_hardening"
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        tables = inspect(connection).get_table_names()
        event_columns = {
            column["name"] for column in inspect(connection).get_columns("account_eligibility_events")
        }
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
        downgraded_tables = inspect(connection).get_table_names()

    assert "task_account_daily_coverage" in tables
    assert "account_eligibility_events" in tables
    assert {"attempt_count", "next_attempt_at"}.issubset(event_columns)
    assert "task_account_daily_coverage" not in downgraded_tables
    assert "account_eligibility_events" not in downgraded_tables
