from datetime import datetime
from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import GenerationTimingBinding


pytestmark = pytest.mark.no_postgres


def test_actual_migration_matches_orm_and_preserves_foreign_keys(monkeypatch):
    migration = import_module("migrations.versions.0216_generation_timing_binding")
    lightweight = import_module("migrations.versions.0219_lightweight_timing")
    engine = create_engine("sqlite:///:memory:")
    parents = ("tasks", "generation_jobs", "execution_timing_profile_revisions", "execution_resilience_policy_revisions")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("CREATE TABLE tenants (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO tenants VALUES (1)")
        for table in parents:
            connection.exec_driver_sql(f"CREATE TABLE {table} (id VARCHAR(36) PRIMARY KEY)")
            connection.exec_driver_sql(f"INSERT INTO {table} VALUES ('qa-id')")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        monkeypatch.setattr(lightweight, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        with Session(connection, join_transaction_mode="create_savepoint") as session:
            session.add(_binding())
            session.commit()
        lightweight.upgrade()
        assert {column["name"]: column["nullable"] for column in inspect(connection).get_columns("generation_timing_bindings")} == {
            column.name: column.nullable for column in GenerationTimingBinding.__table__.columns
        }
        with Session(connection, join_transaction_mode="create_savepoint") as session:
            saved = session.get(GenerationTimingBinding, "qa-id")
            assert saved.timing_profile_id == "qa-id" and saved.profile_snapshot_hash == "b" * 64
            with pytest.raises(IntegrityError), session.begin_nested():
                connection.exec_driver_sql("DELETE FROM execution_timing_profile_revisions WHERE id='qa-id'")
            with pytest.raises(IntegrityError), session.begin_nested():
                connection.exec_driver_sql("UPDATE generation_timing_bindings SET generation_job_id='missing'")
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(_binding())
                session.flush()
            session.commit()
        lightweight.downgrade()
        migration.downgrade()
        assert set(inspect(connection).get_table_names()) == {"tenants", *parents}
        assert connection.exec_driver_sql("SELECT id FROM generation_jobs").scalar_one() == "qa-id"
    engine.dispose()


def _binding():
    return GenerationTimingBinding(
        generation_job_id="qa-id", tenant_id=1, task_id="qa-id", task_lifecycle_epoch=1,
        adapter="group_ai_chat", lane="response", execution_path_hash="a" * 64,
        timing_profile_id="qa-id", profile_snapshot_hash="b" * 64, resilience_policy_id="qa-id",
        llm_timeout_ceiling_seconds=15, bound_send_deadline_at=datetime(2026, 9, 4, 13),
        bound_at=datetime(2026, 9, 4, 12),
    )
