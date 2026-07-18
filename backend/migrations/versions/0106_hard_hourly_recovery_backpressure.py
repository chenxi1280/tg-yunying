"""index hard-hourly recovery backpressure queries

Revision ID: 0106_hard_hourly_recovery_backpressure
Revises: 0105_hard_hourly_checkpoint
Create Date: 2026-07-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0106_hard_hourly_backpressure"
down_revision = "0105_hard_hourly_checkpoint"
branch_labels = None
depends_on = None

INDEX_DEFINITIONS = (
    (
        "ix_actions_recovery_hard_hourly_pending",
        "CREATE INDEX CONCURRENTLY ix_actions_recovery_hard_hourly_pending "
        "ON actions (scheduled_at, created_at) "
        "WHERE task_type = 'group_ai_chat' AND action_type = 'send_message' "
        "AND status IN ('pending', 'claiming') "
        "AND CAST(payload ->> 'hard_hourly_target' AS BOOLEAN) IS TRUE",
        "CREATE INDEX IF NOT EXISTS ix_actions_recovery_hard_hourly_pending "
        "ON actions (scheduled_at, created_at) "
        "WHERE task_type = 'group_ai_chat' AND action_type = 'send_message' "
        "AND status IN ('pending', 'claiming') "
        "AND JSON_EXTRACT(payload, '$.hard_hourly_target') IS 1",
    ),
    (
        "ix_actions_pending_membership_fast_track",
        "CREATE INDEX CONCURRENTLY ix_actions_pending_membership_fast_track "
        "ON actions (scheduled_at, created_at) "
        "WHERE status = 'pending' "
        "AND action_type IN ('ensure_target_membership', 'ensure_channel_membership')",
        "CREATE INDEX IF NOT EXISTS ix_actions_pending_membership_fast_track "
        "ON actions (scheduled_at, created_at) "
        "WHERE status = 'pending' "
        "AND action_type IN ('ensure_target_membership', 'ensure_channel_membership')",
    ),
)
TABLE_NAME = "actions"


def upgrade() -> None:
    _require_table()
    existing = _index_names()
    for index_name, postgres_sql, sqlite_sql in INDEX_DEFINITIONS:
        if index_name not in existing:
            _execute_ddl(postgres_sql if _is_postgres() else sqlite_sql)


def downgrade() -> None:
    _require_table()
    existing = _index_names(valid_only=False)
    for index_name, _postgres_sql, _sqlite_sql in reversed(INDEX_DEFINITIONS):
        if index_name in existing:
            _execute_ddl(f"DROP INDEX {'CONCURRENTLY ' if _is_postgres() else ''}{index_name}")


def _execute_ddl(statement: str) -> None:
    if not _is_postgres():
        op.execute(statement)
        return
    with op.get_context().autocommit_block():
        op.execute(statement)


def _require_table() -> None:
    if TABLE_NAME not in sa.inspect(op.get_bind()).get_table_names():
        raise RuntimeError(f"required table missing: {TABLE_NAME}")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _index_names(*, valid_only: bool = True) -> set[str]:
    bind = op.get_bind()
    if not _is_postgres():
        return {item["name"] for item in sa.inspect(bind).get_indexes(TABLE_NAME)}
    validity_filter = "AND index_meta.indisvalid" if valid_only else ""
    query = sa.text(
        "SELECT index_class.relname FROM pg_index AS index_meta "
        "JOIN pg_class AS table_class ON table_class.oid = index_meta.indrelid "
        "JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid "
        "JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace "
        "WHERE table_class.relname = :table_name AND namespace.nspname = current_schema() "
        f"{validity_filter}"
    )
    return set(bind.execute(query, {"table_name": TABLE_NAME}).scalars())
