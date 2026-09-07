"""Migration checks for the two concrete AI history query access paths."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


pytestmark = pytest.mark.no_postgres


def migration_module():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/0227_ai_group_history_indexes.py"
    spec = importlib.util.spec_from_file_location("ai_group_history_indexes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_history_tables(connection, *, postgres=False):
    payload_type = "JSON" if postgres else "TEXT"
    connection.execute(text(f"""CREATE TABLE actions (
        id VARCHAR PRIMARY KEY, tenant_id INTEGER, task_type VARCHAR,
        status VARCHAR, payload {payload_type}, executed_at TIMESTAMP, created_at TIMESTAMP
    )"""))
    connection.execute(text("""CREATE TABLE group_context_messages (
        id INTEGER PRIMARY KEY, tenant_id INTEGER, group_id INTEGER,
        sent_at TIMESTAMP, created_at TIMESTAMP
    )"""))
    connection.execute(text("""CREATE TABLE fulfillment_remote_facts (
        tenant_id INTEGER, action_id VARCHAR, fact_kind VARCHAR, observed_at TIMESTAMP
    )"""))
    connection.execute(text("""CREATE INDEX ix_test_remote_action ON fulfillment_remote_facts
        (tenant_id, action_id, fact_kind, observed_at)"""))


def test_history_indexes_upgrade_twice_and_downgrade_twice():
    engine = create_engine("sqlite:///:memory:")
    migration = migration_module()
    with engine.begin() as connection:
        create_history_tables(connection)
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        names = set(connection.execute(text("SELECT name FROM sqlite_master WHERE type='index'")).scalars())
        assert {name for name, _ in migration.INDEXES} <= names
        migration.downgrade()
        migration.downgrade()
        names = set(connection.execute(text("SELECT name FROM sqlite_master WHERE type='index'")).scalars())
        assert not {name for name, _ in migration.INDEXES} & names
    engine.dispose()
