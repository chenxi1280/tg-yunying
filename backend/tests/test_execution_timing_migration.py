from dataclasses import replace
from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ExecutionTimingProfileRevision, ExecutionTimingSample
from app.services.task_center.engagement_timing_measurements import record_execution_timing_sample
from app.services.task_center.engagement_timing_profiles import publish_execution_timing_profile
from tests.test_engagement_timing_profiles import _approval, _spec


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def migrated_connection(monkeypatch):
    migration = import_module("migrations.versions.0215_execution_timing_profiles")
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("CREATE TABLE tenants (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE execution_attempts (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO tenants VALUES (1)")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        yield connection, migration
    engine.dispose()


def test_migration_matches_orm_columns_and_unique_constraints(migrated_connection):
    connection, _ = migrated_connection
    inspector = inspect(connection)
    for model in (ExecutionTimingSample, ExecutionTimingProfileRevision):
        columns = inspector.get_columns(model.__tablename__)
        assert {column["name"]: column["nullable"] for column in columns} == {
            column.name: column.nullable for column in model.__table__.columns
        }
        constraints = {item["name"]: tuple(item["column_names"])
                       for item in inspector.get_unique_constraints(model.__tablename__)}
        for constraint in model.__table__.constraints:
            if constraint.__class__.__name__ == "UniqueConstraint":
                assert constraints[constraint.name] == tuple(column.name for column in constraint.columns)


def test_real_migration_supports_profiles_and_enforces_current_scope(migrated_connection):
    connection, migration = migrated_connection
    with Session(bind=connection, autoflush=False, join_transaction_mode="create_savepoint") as session:
        sample = record_execution_timing_sample(session, _spec())
        first = publish_execution_timing_profile(session, _approval([sample]))
        second = publish_execution_timing_profile(session, replace(_approval([sample]), approval_reference="second-review"))
        assert first.state == "superseded" and second.state == "active"
        with pytest.raises(IntegrityError), session.begin_nested():
            connection.exec_driver_sql("UPDATE execution_timing_profile_revisions SET state='active'")
        with pytest.raises(IntegrityError), session.begin_nested():
            connection.exec_driver_sql("UPDATE execution_timing_samples SET execution_attempt_id='missing'")
        with pytest.raises(IntegrityError), session.begin_nested():
            connection.exec_driver_sql("DELETE FROM execution_timing_profile_revisions WHERE id=?", (first.id,))
        session.commit()
    migration.downgrade()
    assert set(inspect(connection).get_table_names()) == {"tenants", "execution_attempts"}
    assert connection.exec_driver_sql("SELECT id FROM tenants").scalar_one() == 1
