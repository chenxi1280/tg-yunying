from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, create_engine, inspect, text


pytestmark = pytest.mark.no_postgres
VERSIONS = Path(__file__).resolve().parents[1] / "migrations/versions"


def test_ai_content_migrations_upgrade_prior_schema_on_sqlite() -> None:
    migration_155 = _migration("0155_ai_content_policy_routes.py", "ai_content_0155")
    migration_156 = _migration("0156_ai_content_runtime.py", "ai_content_0156")
    engine = create_engine("sqlite:///:memory:")
    _prior_metadata().create_all(engine)

    with engine.begin() as connection:
        _seed_legacy_provider(connection)
        operations = Operations(MigrationContext.configure(connection))
        migration_155.op = operations
        migration_156.op = operations
        migration_155.upgrade()
        migration_156.upgrade()
        migration_155.upgrade()
        migration_156.upgrade()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {
        "ai_content_policy_versions",
        "context_scope_revisions",
        "tenant_ai_provider_route_sets",
        "ai_content_window_plan_slots",
        "ai_provider_attempts",
        "fulfillment_shortfall_facts",
        "source_pacing_capacity_plans",
    } <= tables
    assert "credential_enabled" in _column_names(inspector, "ai_providers")
    plan_scope = next(
        item for item in inspector.get_unique_constraints("source_pacing_capacity_plans")
        if item["name"] == "uq_source_capacity_plan_scope"
    )
    assert "revision" in plan_scope["column_names"]
    assert {
        "generation_stage",
        "provider_route_snapshots",
        "window_slot_id",
        "latest_safe_send_at",
    } <= _column_names(inspector, "generation_jobs")
    for table in migration_156.OWNER_TABLES:
        assert {
            "source_capacity_plan_hash",
            "source_capacity_slot_ordinal",
        } <= _column_names(inspector, table)
    with engine.connect() as connection:
        route_count = connection.scalar(text(
            "SELECT count(*) FROM tenant_ai_provider_route_sets WHERE tenant_id = 1"
        ))
        item_count = connection.scalar(text(
            "SELECT count(*) FROM tenant_ai_provider_route_items WHERE provider_id = 7"
        ))
    assert route_count == len(migration_155.LEGACY_GROUP_PURPOSES)
    assert item_count == route_count


def _migration(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, VERSIONS / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"migration_load_failed:{filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _column_names(inspector, table: str) -> set[str]:  # noqa: ANN001
    return {str(item["name"]) for item in inspector.get_columns(table)}


def _prior_metadata() -> MetaData:
    metadata = MetaData()
    _identity_tables(metadata)
    _legacy_ai_tables(metadata)
    _legacy_generation_table(metadata)
    for name in (
        "task_group_daily_message_slots",
        "comment_fulfillment_obligations",
        "reaction_fulfillment_obligations",
        "view_fulfillment_obligations",
    ):
        Table(name, metadata, Column("id", String(36), primary_key=True))
    return metadata


def _identity_tables(metadata: MetaData) -> None:
    Table("tenants", metadata, Column("id", Integer, primary_key=True))
    Table("app_users", metadata, Column("id", Integer, primary_key=True))
    Table("tasks", metadata, Column("id", String(36), primary_key=True))
    Table("tg_accounts", metadata, Column("id", Integer, primary_key=True))


def _legacy_ai_tables(metadata: MetaData) -> None:
    Table(
        "ai_providers",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("is_active", Boolean, nullable=False),
        Column("model_name", String(120), nullable=False),
    )
    Table(
        "tenant_ai_settings",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Integer, nullable=False),
        Column("default_provider_id", Integer),
    )


def _seed_legacy_provider(connection) -> None:  # noqa: ANN001
    connection.execute(text(
        "INSERT INTO tenants (id) VALUES (1)"
    ))
    connection.execute(text(
        "INSERT INTO ai_providers (id, is_active, model_name) "
        "VALUES (7, true, 'deepseek-chat')"
    ))
    connection.execute(text(
        "INSERT INTO tenant_ai_settings (id, tenant_id, default_provider_id) "
        "VALUES (1, 1, 7)"
    ))


def _legacy_generation_table(metadata: MetaData) -> None:
    Table(
        "generation_jobs",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("state", String(24), nullable=False),
    )
