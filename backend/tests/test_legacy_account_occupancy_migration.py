from importlib import import_module

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect, text

from app.database import Base
from migrations.legacy_bootstrap import legacy_bootstrap_metadata


pytestmark = pytest.mark.no_postgres


def test_index_upgrade_and_downgrade_preserve_unknown_attempt(monkeypatch):
    migration = import_module("migrations.versions.0224_legacy_account_occupancy")
    with create_engine("sqlite:///:memory:").begin() as connection:
        connection.exec_driver_sql("CREATE TABLE execution_attempts (id TEXT, tenant_id INTEGER, "
            "account_id INTEGER, gateway_call_started_at DATETIME, status TEXT)")
        connection.execute(text("INSERT INTO execution_attempts VALUES (:id, 1, 11, :at, :status)"),
            {"id": "original", "at": "2026-09-05 10:00:00", "status": "result_unknown"})
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        indexes = inspect(connection).get_indexes("execution_attempts")
        assert indexes[0]["name"] == migration.INDEX_NAME
        assert indexes[0]["column_names"] == ["tenant_id", "account_id", "gateway_call_started_at"]
        migration.downgrade()
        migration.downgrade()
        assert not inspect(connection).get_indexes("execution_attempts")
        assert connection.execute(text("SELECT id, status FROM execution_attempts")).one() == (
            "original", "result_unknown")


def test_bootstrap_excludes_later_index_without_mutating_live_metadata():
    name = "ix_execution_attempts_account_usage"
    assert name in {index.name for index in Base.metadata.tables["execution_attempts"].indexes}
    boot = legacy_bootstrap_metadata(Base.metadata)
    assert name not in {index.name for index in boot.tables["execution_attempts"].indexes}
    assert name in {index.name for index in Base.metadata.tables["execution_attempts"].indexes}
