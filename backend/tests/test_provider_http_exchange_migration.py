from datetime import datetime
from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ProviderHttpExchange, ProviderHttpExchangeJob


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("occupied", (False, True))
def test_real_migration_matches_models_and_preserves_execution_evidence(monkeypatch, occupied):
    migration = import_module("migrations.versions.0217_provider_http_exchanges")
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        _parents(connection)
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        for model in (ProviderHttpExchange, ProviderHttpExchangeJob):
            columns = inspect(connection).get_columns(model.__tablename__)
            assert {item["name"]: item["nullable"] for item in columns} == {
                item.name: item.nullable for item in model.__table__.columns}
        if occupied:
            _populate_and_check_foreign_keys(connection)
            with pytest.raises(RuntimeError, match="discard_execution_evidence"):
                migration.downgrade()
            assert connection.exec_driver_sql("SELECT outcome FROM provider_http_exchanges").scalar_one() == "unknown"
        else:
            migration.downgrade()
            assert "provider_http_exchanges" not in inspect(connection).get_table_names()
        assert connection.exec_driver_sql("SELECT id FROM tasks").scalar_one() == "task"
    engine.dispose()


def _parents(connection):
    for table in ("tenants", "ai_providers"):
        connection.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(f"INSERT INTO {table} VALUES (1)")
    connection.exec_driver_sql("CREATE TABLE tasks (id VARCHAR(36) PRIMARY KEY)")
    connection.exec_driver_sql("INSERT INTO tasks VALUES ('task')")
    connection.exec_driver_sql("CREATE TABLE generation_timing_bindings (generation_job_id VARCHAR(36) PRIMARY KEY)")
    connection.exec_driver_sql("INSERT INTO generation_timing_bindings VALUES ('job')")


def _populate_and_check_foreign_keys(connection):
    with Session(connection, join_transaction_mode="create_savepoint") as session:
        session.add(ProviderHttpExchange(id="exchange", chain_id="chain", tenant_id=1, task_id="task",
            task_lifecycle_epoch=1, provider_id=1, logical_request_id="logical", model_name="QA", purpose="QA",
            request_hash="a" * 64, outcome="unknown", started_at=datetime(2026, 9, 4, 12)))
        session.flush()
        session.add(ProviderHttpExchangeJob(exchange_id="exchange", generation_job_id="job", execution_path_hash="b" * 64))
        session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            connection.exec_driver_sql("DELETE FROM generation_timing_bindings WHERE generation_job_id='job'")
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(ProviderHttpExchangeJob(exchange_id="exchange", generation_job_id="missing", execution_path_hash="c" * 64))
            session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            connection.exec_driver_sql("DELETE FROM provider_http_exchanges WHERE id='exchange'")
        session.commit()
