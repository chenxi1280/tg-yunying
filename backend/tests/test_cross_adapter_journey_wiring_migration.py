from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError


pytestmark = pytest.mark.no_postgres


def test_comment_journey_migration_preserves_rows_and_enforces_reference(monkeypatch) -> None:
    migration = import_module("migrations.versions.0214_cross_adapter_journey_wiring")
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("CREATE TABLE cross_adapter_source_journey_plan_revisions (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE channel_comment_plan_contracts (id VARCHAR(36) PRIMARY KEY, payload TEXT)")
        connection.exec_driver_sql("INSERT INTO channel_comment_plan_contracts VALUES ('legacy', 'keep')")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        row = connection.exec_driver_sql("SELECT payload, source_journey_plan_id FROM channel_comment_plan_contracts").one()
        assert row == ("keep", None)
        connection.exec_driver_sql("INSERT INTO cross_adapter_source_journey_plan_revisions VALUES ('journey')")
        connection.exec_driver_sql("UPDATE channel_comment_plan_contracts SET source_journey_plan_id='journey'")
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql("DELETE FROM cross_adapter_source_journey_plan_revisions WHERE id='journey'")
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql("UPDATE channel_comment_plan_contracts SET source_journey_plan_id='missing'")
        migration.downgrade()
        assert {row['name'] for row in inspect(connection).get_columns('channel_comment_plan_contracts')} == {'id', 'payload'}
        assert connection.exec_driver_sql("SELECT id, payload FROM channel_comment_plan_contracts").one() == ("legacy", "keep")
