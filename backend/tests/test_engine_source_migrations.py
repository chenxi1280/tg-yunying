from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from app.models import AlbumReactionParticipation, ChannelSourceDecision, ChannelTaskIntake


pytestmark = pytest.mark.no_postgres


def test_source_and_album_migrations_match_models_and_preserve_existing_message(monkeypatch):
    migrations = [import_module(f"migrations.versions.{name}") for name in (
        "0220_channel_source_intake", "0221_album_reaction")]
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        for table in ("tenants", "tg_accounts", "operation_targets", "channel_messages"):
            connection.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        for table in ("tasks", "task_day_ledgers"):
            connection.exec_driver_sql(f"CREATE TABLE {table} (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO channel_messages VALUES (1)")
        for migration in migrations:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration.upgrade()
        for model in (AlbumReactionParticipation, ChannelSourceDecision, ChannelTaskIntake):
            actual = {c["name"]: c["nullable"] for c in inspect(connection).get_columns(model.__tablename__)}
            assert actual == {c.name: c.nullable for c in model.__table__.columns}
            assert inspect(connection).get_unique_constraints(model.__tablename__)
            assert inspect(connection).get_foreign_keys(model.__tablename__)
        assert connection.exec_driver_sql("SELECT id, grouped_id, source_metadata FROM channel_messages").one() == (1, "", "{}")
        for migration in reversed(migrations):
            migration.downgrade()
        assert connection.exec_driver_sql("SELECT id FROM channel_messages").scalar_one() == 1
