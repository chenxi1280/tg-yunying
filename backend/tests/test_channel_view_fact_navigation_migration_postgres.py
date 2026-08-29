from __future__ import annotations

from contextlib import contextmanager
import importlib
import os
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


pytestmark = pytest.mark.isolated_postgres

MIGRATION_MODULE = "migrations.versions.0173_channel_view_fact_navigation"
TARGET_FK = "fk_view_remote_fact_obligation_navigation"

RELEASED_0172_SCHEMA_SQL = """
CREATE TABLE view_fulfillment_obligations (id VARCHAR(36) PRIMARY KEY);
CREATE TABLE view_remote_facts (
    id VARCHAR(36) PRIMARY KEY,
    obligation_id VARCHAR(36) NOT NULL
        REFERENCES view_fulfillment_obligations(id) ON DELETE CASCADE
);
INSERT INTO view_fulfillment_obligations VALUES ('obligation-one');
INSERT INTO view_remote_facts VALUES ('fact-one', 'obligation-one');
"""


def test_released_0172_schema_upgrades_and_safely_downgrades() -> None:
    with _migration_schema() as connection:
        connection.exec_driver_sql(RELEASED_0172_SCHEMA_SQL)
        migration = _bound_migration(connection)

        migration.upgrade()
        migration.upgrade()
        _assert_contract(connection, nullable=True, ondelete="SET NULL", name=TARGET_FK)

        migration.downgrade()
        _assert_contract(
            connection,
            nullable=False,
            ondelete="CASCADE",
            name="view_remote_facts_obligation_id_fkey",
        )
        connection.execute(text(
            "DELETE FROM view_fulfillment_obligations WHERE id = 'obligation-one'"
        ))
        assert connection.execute(text(
            "SELECT COUNT(*) FROM view_remote_facts"
        )).scalar_one() == 0


def test_downgrade_refuses_null_fact_navigation() -> None:
    with _migration_schema() as connection:
        connection.exec_driver_sql(RELEASED_0172_SCHEMA_SQL)
        migration = _bound_migration(connection)
        migration.upgrade()
        connection.execute(text(
            "DELETE FROM view_fulfillment_obligations WHERE id = 'obligation-one'"
        ))

        with pytest.raises(
            RuntimeError,
            match="channel_view_fact_navigation_downgrade_unsafe:1",
        ):
            migration.downgrade()

        _assert_contract(connection, nullable=True, ondelete="SET NULL", name=TARGET_FK)
        assert connection.execute(text("""
            SELECT COUNT(*) FROM view_remote_facts WHERE obligation_id IS NULL
        """)).scalar_one() == 1


@contextmanager
def _migration_schema():
    database_url = os.environ["TEST_DATABASE_URL"]
    schema = f"test_view_fact_nav_{uuid4().hex}"
    engine = create_engine(database_url, future=True, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.dialect.default_schema_name = schema
            yield connection
        finally:
            connection.execute(text("SET search_path TO public"))
            connection.dialect.default_schema_name = "public"
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


def _bound_migration(connection):
    migration = importlib.import_module(MIGRATION_MODULE)
    migration.op = Operations(MigrationContext.configure(connection))
    return migration


def _assert_contract(connection, *, nullable: bool, ondelete: str, name: str) -> None:
    inspector = inspect(connection)
    columns = {column["name"]: column for column in inspector.get_columns("view_remote_facts")}
    foreign_keys = [
        foreign_key
        for foreign_key in inspector.get_foreign_keys("view_remote_facts")
        if foreign_key["constrained_columns"] == ["obligation_id"]
    ]
    assert bool(columns["obligation_id"]["nullable"]) is nullable
    assert len(foreign_keys) == 1
    assert foreign_keys[0]["name"] == name
    assert foreign_keys[0]["options"]["ondelete"].upper() == ondelete
