from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, UniqueConstraint, create_engine, select


pytestmark = pytest.mark.no_postgres


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0017_operations_center.py"


def test_operations_center_backfill_includes_required_target_username() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    _create_existing_tables(engine)

    with engine.begin() as connection:
        connection.execute(
            Table("tg_groups", MetaData(), autoload_with=connection).insert(),
            {
                "id": 1,
                "tenant_id": 1,
                "group_type": "group",
                "tg_peer_id": "-1001",
                "title": "测试群",
                "member_count": 1,
                "can_send": True,
                "auth_status": "已授权运营",
            },
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        targets = Table("operation_targets", MetaData(), autoload_with=connection)
        row = connection.execute(select(targets.c.username, targets.c.title)).one()

    assert row.username == ""
    assert row.title == "测试群"


def _migration_module():
    spec = importlib.util.spec_from_file_location("operations_center_0017", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_existing_tables(engine) -> None:
    metadata = MetaData()
    Table(
        "tg_groups",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Integer, nullable=False),
        Column("group_type", String(20), nullable=False),
        Column("tg_peer_id", String(120), nullable=False),
        Column("title", String(180), nullable=False),
        Column("member_count", Integer, nullable=False),
        Column("can_send", Integer, nullable=False),
        Column("auth_status", String(30), nullable=False),
    )
    Table(
        "operation_targets",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Integer, nullable=False),
        Column("target_type", String(20), nullable=False),
        Column("tg_peer_id", String(120), nullable=False),
        Column("title", String(180), nullable=False),
        Column("username", String(120), nullable=False),
        Column("member_count", Integer, nullable=False),
        Column("can_send", Integer, nullable=False),
        Column("auth_status", String(30), nullable=False),
        Column("last_sync_at", String(40)),
        Column("created_at", String(40), nullable=False),
        Column("updated_at", String(40), nullable=False),
        UniqueConstraint("tenant_id", "tg_peer_id", name="uq_operation_targets_tenant_peer"),
    )
    for table_name in (
        "channel_messages",
        "operation_tasks",
        "operation_task_attempts",
        "manual_operation_records",
    ):
        Table(table_name, metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)
