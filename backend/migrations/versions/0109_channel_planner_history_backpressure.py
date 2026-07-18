"""index bounded channel planner history lookups

Revision ID: 0109_channel_planner_history
Revises: 0108_runtime_retention_idx
Create Date: 2026-07-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0109_channel_planner_history"
down_revision = "0108_runtime_retention_idx"
branch_labels = None
depends_on = None

ACTION_TABLE = "actions"
MESSAGE_HISTORY_INDEX = "ix_actions_channel_planner_message_history"
LEGACY_HISTORY_INDEX = "ix_actions_channel_planner_legacy_history"
DAILY_CAPACITY_INDEX = "ix_actions_channel_view_daily_capacity"
INDEXES = (
    (MESSAGE_HISTORY_INDEX, "channel_message_id"),
    (LEGACY_HISTORY_INDEX, "message_id"),
)


def upgrade() -> None:
    _require_table()
    _create_history_indexes()
    _create_daily_capacity_index()


def downgrade() -> None:
    _require_table()
    _drop_index(DAILY_CAPACITY_INDEX)
    for index_name, _payload_key in reversed(INDEXES):
        _drop_index(index_name)


def _create_history_indexes() -> None:
    existing = _index_names()
    for index_name, payload_key in INDEXES:
        if index_name not in existing:
            _execute_ddl(_history_index_sql(index_name, payload_key))


def _create_daily_capacity_index() -> None:
    if DAILY_CAPACITY_INDEX not in _index_names():
        _execute_ddl(_daily_capacity_index_sql())


def _drop_index(index_name: str) -> None:
    if index_name in _index_names(valid_only=False):
        _execute_ddl(f"DROP INDEX {'CONCURRENTLY ' if _is_postgres() else ''}{index_name}")


def _history_index_sql(index_name: str, payload_key: str) -> str:
    payload_expression = _payload_expression(payload_key)
    return (
        f"CREATE INDEX {'CONCURRENTLY ' if _is_postgres() else 'IF NOT EXISTS '}{index_name} ON {ACTION_TABLE} "
        f"(task_id, action_type, status, {payload_expression}, account_id) "
        "WHERE action_type IN ('view_message', 'like_message')"
    )


def _daily_capacity_index_sql() -> str:
    return (
        f"CREATE INDEX {'CONCURRENTLY ' if _is_postgres() else 'IF NOT EXISTS '}{DAILY_CAPACITY_INDEX} ON {ACTION_TABLE} "
        f"(task_id, {_payload_expression('execution_date')}, account_id) "
        "WHERE action_type = 'view_message' AND status IN ('pending', 'executing', 'success')"
    )


def _payload_expression(payload_key: str) -> str:
    if _is_postgres():
        return f"((payload ->> '{payload_key}')::varchar)"
    return f"json_extract(payload, '$.{payload_key}')"


def _execute_ddl(statement: str) -> None:
    if not _is_postgres():
        op.execute(statement)
        return
    with op.get_context().autocommit_block():
        op.execute(statement)


def _require_table() -> None:
    if ACTION_TABLE not in sa.inspect(op.get_bind()).get_table_names():
        raise RuntimeError(f"required table missing: {ACTION_TABLE}")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _index_names(*, valid_only: bool = True) -> set[str]:
    bind = op.get_bind()
    if not _is_postgres():
        return {row[1] for row in bind.execute(sa.text(f"PRAGMA index_list({ACTION_TABLE})"))}
    validity_filter = "AND index_meta.indisvalid" if valid_only else ""
    query = sa.text(
        "SELECT index_class.relname FROM pg_index AS index_meta "
        "JOIN pg_class AS table_class ON table_class.oid = index_meta.indrelid "
        "JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid "
        "JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace "
        "WHERE table_class.relname = :table_name AND namespace.nspname = current_schema() "
        f"{validity_filter}"
    )
    return set(bind.execute(query, {"table_name": ACTION_TABLE}).scalars())
