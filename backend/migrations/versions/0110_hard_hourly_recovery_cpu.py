"""index hard-hourly history and stale worker lease lookup

Revision ID: 0110_hard_hourly_recovery_cpu
Revises: 0109_channel_planner_history
Create Date: 2026-07-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0110_hard_hourly_recovery_cpu"
down_revision = "0109_channel_planner_history"
branch_labels = None
depends_on = None

ACTION_TABLE = "actions"
HEARTBEAT_TABLE = "worker_heartbeats"
INDEXES = (
    (
        ACTION_TABLE,
        "ix_actions_hard_hourly_history_executed",
        "CREATE INDEX CONCURRENTLY ix_actions_hard_hourly_history_executed "
        "ON actions (tenant_id, task_id, executed_at) INCLUDE (id, status, account_id, scheduled_at) "
        "WHERE task_type = 'group_ai_chat' AND action_type = 'send_message'",
        "CREATE INDEX IF NOT EXISTS ix_actions_hard_hourly_history_executed "
        "ON actions (tenant_id, task_id, executed_at) "
        "WHERE task_type = 'group_ai_chat' AND action_type = 'send_message'",
    ),
    (
        ACTION_TABLE,
        "ix_actions_hard_hourly_history_scheduled",
        "CREATE INDEX CONCURRENTLY ix_actions_hard_hourly_history_scheduled "
        "ON actions (tenant_id, task_id, scheduled_at) INCLUDE (id, status, account_id, executed_at) "
        "WHERE task_type = 'group_ai_chat' AND action_type = 'send_message'",
        "CREATE INDEX IF NOT EXISTS ix_actions_hard_hourly_history_scheduled "
        "ON actions (tenant_id, task_id, scheduled_at) "
        "WHERE task_type = 'group_ai_chat' AND action_type = 'send_message'",
    ),
    (
        ACTION_TABLE,
        "ix_actions_executing_lease_owner",
        "CREATE INDEX CONCURRENTLY ix_actions_executing_lease_owner ON actions (lease_owner) "
        "WHERE status = 'executing' AND lease_owner <> ''",
        "CREATE INDEX IF NOT EXISTS ix_actions_executing_lease_owner ON actions (lease_owner) "
        "WHERE status = 'executing' AND lease_owner <> ''",
    ),
    (
        HEARTBEAT_TABLE,
        "ix_worker_heartbeats_host_pid_last_seen_at",
        "CREATE INDEX CONCURRENTLY ix_worker_heartbeats_host_pid_last_seen_at "
        "ON worker_heartbeats (hostname, pid, last_seen_at) INCLUDE (worker_id)",
        "CREATE INDEX IF NOT EXISTS ix_worker_heartbeats_host_pid_last_seen_at "
        "ON worker_heartbeats (hostname, pid, last_seen_at)",
    ),
)


def upgrade() -> None:
    _require_tables()
    for table_name, index_name, postgres_sql, sqlite_sql in INDEXES:
        if index_name not in _index_names(table_name):
            _execute_ddl(postgres_sql if _is_postgres() else sqlite_sql)


def downgrade() -> None:
    _require_tables()
    for table_name, index_name, _postgres_sql, _sqlite_sql in reversed(INDEXES):
        if index_name in _index_names(table_name, valid_only=False):
            _execute_ddl(f"DROP INDEX {'CONCURRENTLY ' if _is_postgres() else ''}{index_name}")


def _require_tables() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    missing = sorted({ACTION_TABLE, HEARTBEAT_TABLE} - tables)
    if missing:
        raise RuntimeError(f"required tables missing: {', '.join(missing)}")


def _execute_ddl(statement: str) -> None:
    if not _is_postgres():
        op.execute(statement)
        return
    with op.get_context().autocommit_block():
        op.execute(statement)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _index_names(table_name: str, *, valid_only: bool = True) -> set[str]:
    bind = op.get_bind()
    if not _is_postgres():
        return {row[1] for row in bind.execute(sa.text(f"PRAGMA index_list({table_name})"))}
    validity_filter = "AND index_meta.indisvalid" if valid_only else ""
    query = sa.text(
        "SELECT index_class.relname FROM pg_index AS index_meta "
        "JOIN pg_class AS table_class ON table_class.oid = index_meta.indrelid "
        "JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid "
        "JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace "
        "WHERE table_class.relname = :table_name AND namespace.nspname = current_schema() "
        f"{validity_filter}"
    )
    return set(bind.execute(query, {"table_name": table_name}).scalars())
