from __future__ import annotations

import importlib
import os
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


pytestmark = pytest.mark.isolated_postgres

MIGRATION_MODULE = "migrations.versions.0172_channel_view_daily_fact"

OLD_SCHEMA_SQL = """
CREATE TABLE tenants (id INTEGER PRIMARY KEY);
CREATE TABLE tg_accounts (id INTEGER PRIMARY KEY);
CREATE TABLE tasks (id VARCHAR(36) PRIMARY KEY);
CREATE TABLE task_day_ledgers (
    id VARCHAR(36) PRIMARY KEY,
    task_id VARCHAR(36) NOT NULL,
    obligation_local_date DATE NOT NULL
);
CREATE TABLE actions (id VARCHAR(36) PRIMARY KEY, status VARCHAR(24) NOT NULL);
CREATE TABLE operation_targets (id INTEGER PRIMARY KEY, tg_peer_id VARCHAR(120));
CREATE TABLE channel_messages (
    id INTEGER PRIMARY KEY,
    channel_target_id INTEGER REFERENCES operation_targets(id)
);
CREATE TABLE execution_attempts (
    id VARCHAR(36) PRIMARY KEY,
    action_id VARCHAR(36),
    gateway_call_started_at TIMESTAMPTZ
);
CREATE TABLE view_fulfillment_obligations (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    task_day_ledger_id VARCHAR(36) NOT NULL REFERENCES task_day_ledgers(id),
    channel_message_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL REFERENCES tg_accounts(id),
    current_action_id VARCHAR(36) REFERENCES actions(id),
    status VARCHAR(24) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE view_remote_facts (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    obligation_id VARCHAR(36) NOT NULL,
    target_peer_id VARCHAR(120) NOT NULL,
    channel_message_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL REFERENCES tg_accounts(id),
    remote_confirmed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT fk_view_remote_fact_obligation_navigation
        FOREIGN KEY (obligation_id)
        REFERENCES view_fulfillment_obligations(id) ON DELETE CASCADE,
    CONSTRAINT uq_view_remote_fact_lifetime_source
        UNIQUE (target_peer_id, channel_message_id, account_id)
);
"""

SEED_FIRST_DAY_SQL = """
INSERT INTO tenants VALUES (1);
INSERT INTO tg_accounts VALUES (11);
INSERT INTO tasks VALUES ('task-one'), ('task-two');
INSERT INTO task_day_ledgers VALUES
    ('ledger-one', 'task-one', DATE '2026-08-29'),
    ('ledger-two', 'task-two', DATE '2026-08-30');
INSERT INTO actions VALUES ('action-one', 'succeeded'), ('action-two', 'succeeded');
INSERT INTO view_fulfillment_obligations VALUES
    ('obligation-one', 1, 'ledger-one', 101, 11, 'action-one',
     'confirmed', TIMESTAMPTZ '2026-08-29 02:00:00+00'),
    ('obligation-two', 1, 'ledger-two', 101, 11, 'action-two',
     'confirmed', TIMESTAMPTZ '2026-08-30 02:00:00+00');
INSERT INTO view_remote_facts VALUES
    ('fact-one', 1, 'obligation-one', '-10001', 101, 11,
     TIMESTAMPTZ '2026-08-29 02:10:00+00',
     TIMESTAMPTZ '2026-08-29 02:10:00+00');
"""

SEED_SECOND_DAY_SQL = """
INSERT INTO view_remote_facts (
    id, tenant_id, obligation_id, obligation_local_date, target_peer_id,
    channel_message_id, account_id, remote_effect_kind,
    counter_increment_proven, remote_confirmed_at, created_at
) VALUES (
    'fact-two', 1, 'obligation-two', DATE '2026-08-30', '-10001',
    101, 11, 'daily_view_operation', false,
    TIMESTAMPTZ '2026-08-30 02:10:00+00',
    TIMESTAMPTZ '2026-08-30 02:10:00+00'
);
INSERT INTO channel_view_daily_identity_owners (
    id, tenant_id, target_peer_id, channel_message_id, account_id,
    obligation_local_date, state, logical_task_id, obligation_id, action_id,
    request_identity, version
) VALUES (
    'owner-two', 1, '-10001', 101, 11, DATE '2026-08-30', 'confirmed',
    'task-two', 'obligation-two', 'action-two', 'task-two:obligation-two', 1
);
"""


def test_daily_fact_downgrade_upgrade_restores_audit_without_navigation() -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    schema = f"test_daily_fact_{uuid4().hex}"
    engine = create_engine(database_url, future=True, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.dialect.default_schema_name = schema
            _run_roundtrip(connection)
        finally:
            connection.execute(text("SET search_path TO public"))
            connection.dialect.default_schema_name = "public"
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


def _run_roundtrip(connection) -> None:
    connection.exec_driver_sql(OLD_SCHEMA_SQL)
    connection.exec_driver_sql(SEED_FIRST_DAY_SQL)
    migration = _bound_migration(connection)
    migration.upgrade()
    connection.exec_driver_sql(SEED_SECOND_DAY_SQL)
    migration.downgrade()
    _assert_downgraded(connection)
    connection.execute(text("DELETE FROM view_fulfillment_obligations"))
    connection.execute(text("DELETE FROM actions"))
    migration.upgrade()
    _assert_restored(connection)


def _bound_migration(connection):
    migration = importlib.import_module(MIGRATION_MODULE)
    migration.op = Operations(MigrationContext.configure(connection))
    return migration


def _assert_downgraded(connection) -> None:
    inspector = inspect(connection)
    assert "obligation_local_date" not in {
        column["name"] for column in inspector.get_columns("view_remote_facts")
    }
    assert connection.execute(text(
        "SELECT COUNT(*) FROM view_remote_fact_daily_rollback_archive"
    )).scalar_one() == 2
    assert connection.execute(text(
        "SELECT COUNT(*) FROM channel_view_daily_owner_rollback_archive"
    )).scalar_one() == 2
    assert connection.execute(text("SELECT COUNT(*) FROM view_remote_facts")).scalar_one() == 1


def _assert_restored(connection) -> None:
    rows = connection.execute(text("""
        SELECT id, obligation_local_date, obligation_id
        FROM view_remote_facts ORDER BY obligation_local_date
    """)).all()
    assert [(row.id, row.obligation_id) for row in rows] == [
        ("fact-one", None),
        ("fact-two", None),
    ]
    assert [str(row.obligation_local_date) for row in rows] == ["2026-08-29", "2026-08-30"]
    owner_rows = connection.execute(text("""
        SELECT obligation_id, action_id
        FROM channel_view_daily_identity_owners ORDER BY obligation_local_date
    """)).all()
    assert owner_rows == [(None, None), (None, None)]
    tables = set(inspect(connection).get_table_names())
    assert "view_remote_fact_daily_rollback_archive" not in tables
    assert "channel_view_daily_owner_rollback_archive" not in tables
