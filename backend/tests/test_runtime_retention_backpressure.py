from __future__ import annotations

import importlib.util
from pathlib import Path
from subprocess import run

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from datetime import date, datetime

from sqlalchemy import Column, DateTime, JSON, MetaData, String, Table, create_engine, text
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, Task, Tenant
from app.services.task_center.runtime_retention import cleanup_runtime_details


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0108_runtime_retention_backpressure.py"


def test_runtime_retention_backpressure_migration_declares_expression_indexes() -> None:
    assert MIGRATION_PATH.exists(), "missing runtime retention backpressure migration"

    source = MIGRATION_PATH.read_text()
    for expected in (
        "ix_actions_runtime_detail_retention",
        "COALESCE(executed_at, scheduled_at, created_at)",
        "created_at, id",
        "ix_runtime_cleanup_audits_kind_created_at",
        "CAST(summary ->> 'cleanup_kind' AS varchar)",
    ):
        assert expected in source


def test_runtime_retention_backpressure_migration_is_idempotent_and_reversible_on_sqlite() -> None:
    migration = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    _legacy_metadata().create_all(engine)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        upgraded = _sqlite_index_names(connection)
        migration.downgrade()
        migration.downgrade()
        downgraded = _sqlite_index_names(connection)

    assert upgraded == {migration.ACTION_INDEX, migration.AUDIT_INDEX}
    assert downgraded == set()


def test_runtime_retention_batch_orders_by_indexed_age() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "task_center"
        / "runtime_retention.py"
    ).read_text()

    assert ".order_by(age.asc(), Action.created_at.asc(), Action.id.asc())" in source


def test_server_runtime_cleanup_defaults_and_legacy_upgrade_match() -> None:
    compose = (PROJECT_ROOT / "docker-compose.server.yml").read_text()
    env_example = (PROJECT_ROOT / ".env.production.example").read_text()
    installer = PROJECT_ROOT / "deploy/server-install-release.sh"
    source = installer.read_text()

    for expected in (
        "RUNTIME_DETAIL_RETENTION_BATCH_SIZE: ${RUNTIME_DETAIL_RETENTION_BATCH_SIZE:-2000}",
        "RUNTIME_DETAIL_CLEANUP_INTERVAL_SECONDS: ${RUNTIME_DETAIL_CLEANUP_INTERVAL_SECONDS:-60}",
        "RUNTIME_METRIC_CLEANUP_INTERVAL_SECONDS: ${RUNTIME_METRIC_CLEANUP_INTERVAL_SECONDS:-300}",
    ):
        assert expected in compose
    assert "RUNTIME_DETAIL_RETENTION_BATCH_SIZE=2000" in env_example
    assert "RUNTIME_DETAIL_CLEANUP_INTERVAL_SECONDS=60" in env_example
    assert "RUNTIME_METRIC_CLEANUP_INTERVAL_SECONDS=300" in env_example
    assert "upgrade_legacy_runtime_cleanup_interval" in source
    assert "RUNTIME_METRIC_CLEANUP_INTERVAL_SECONDS=60" in source
    run(["bash", "-n", str(installer)], check=True)


def test_runtime_retention_preserves_open_actions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    old_at = datetime(2000, 1, 1, 10, 0)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add(Task(
            id="task",
            tenant_id=1,
            name="task",
            type="group_relay",
            status="running",
        ))
        session.add_all([
            _retention_action("open", "pending", old_at),
            _retention_action("terminal", "success", old_at),
        ])
        session.commit()

        cleanup_runtime_details(
            session,
            retention_days=5,
            today=date(2000, 1, 10),
            batch_size=10,
        )
        session.commit()

        assert session.get(Action, "open") is not None
        assert session.get(Action, "terminal") is None


def _retention_action(
    action_id: str,
    status: str,
    timestamp: datetime,
) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="task",
        task_type="group_relay",
        action_type="send_message",
        status=status,
        scheduled_at=timestamp,
        executed_at=timestamp if status == "success" else None,
        created_at=timestamp,
    )


def _migration_module():
    spec = importlib.util.spec_from_file_location("runtime_retention_backpressure_0108", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_metadata() -> MetaData:
    metadata = MetaData()
    Table(
        "actions",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("executed_at", DateTime),
        Column("scheduled_at", DateTime),
        Column("created_at", DateTime),
    )
    Table(
        "runtime_cleanup_audits",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("summary", JSON),
        Column("created_at", DateTime),
    )
    return metadata


def _sqlite_index_names(connection) -> set[str]:
    return set(connection.execute(text(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND name NOT LIKE 'sqlite_autoindex_%'"
    )).scalars())
