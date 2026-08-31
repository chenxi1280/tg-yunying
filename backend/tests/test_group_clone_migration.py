from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

pytestmark = pytest.mark.no_postgres


def test_snapshot_migration_backfills_task_config() -> None:
    migration = _migration_module()
    engine, tasks, events = _database()
    frozen = {"content": {"rule_set_id": 31, "rule_set_version": 1}}
    with engine.begin() as connection:
        connection.execute(tasks.insert().values(id="task-1", type_config=frozen))
        connection.execute(events.insert().values(id="event-1", task_id="task-1"))
        migration._backfill_config_snapshots(connection)
        snapshot = connection.scalar(sa.select(events.c.config_snapshot))
    assert snapshot == frozen


def test_snapshot_migration_rejects_empty_task_config() -> None:
    migration = _migration_module()
    engine, tasks, events = _database()
    with engine.begin() as connection:
        connection.execute(tasks.insert().values(id="task-1", type_config={}))
        connection.execute(events.insert().values(id="event-1", task_id="task-1"))
        with pytest.raises(RuntimeError, match="backfill incomplete"):
            migration._backfill_config_snapshots(connection)


def test_upgrade_accepts_columns_created_by_current_metadata() -> None:
    migration = _migration_module()
    engine, tasks, events = _current_schema_database()
    frozen = {"content": {"rule_set_id": 31, "rule_set_version": 1}}
    with engine.begin() as connection:
        connection.execute(tasks.insert().values(id="task-1", type_config=frozen))
        connection.execute(events.insert().values(
            id="event-1", task_id="task-1", config_snapshot=frozen, sender_name="sender",
        ))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
    columns = {item["name"]: item for item in sa.inspect(engine).get_columns("clone_source_events")}
    assert columns["config_snapshot"]["nullable"] is False
    assert columns["sender_name"]["nullable"] is False


def _database():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    tasks = sa.Table(
        "tasks", metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("type_config", sa.JSON(), nullable=False),
    )
    events = sa.Table(
        "clone_source_events", metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=True),
    )
    metadata.create_all(engine)
    return engine, tasks, events


def _current_schema_database():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    tasks = sa.Table(
        "tasks", metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("type_config", sa.JSON(), nullable=False),
    )
    events = sa.Table(
        "clone_source_events", metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("sender_name", sa.String(160), nullable=False),
    )
    metadata.create_all(engine)
    return engine, tasks, events


def _migration_module():
    path = Path(__file__).parents[1] / "migrations/versions/0183_group_clone_event_config_snapshot.py"
    spec = importlib.util.spec_from_file_location("clone_snapshot_migration", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("clone snapshot migration module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
