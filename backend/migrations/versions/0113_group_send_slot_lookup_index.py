"""index group send slot action lookup

Revision ID: 0113_group_send_slot_lookup
Revises: 0112_task_scope_delete_cascade
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0113_group_send_slot_lookup"
down_revision = "0112_task_scope_delete_cascade"
branch_labels = None
depends_on = None

TABLE_NAME = "actions"
INDEX_NAME = "ix_actions_send_group_slot_lookup"
POSTGRES_CREATE = (
    f"CREATE INDEX CONCURRENTLY {INDEX_NAME} "
    "ON actions (tenant_id, (CAST(payload ->> 'group_id' AS INTEGER)), id) "
    "WHERE action_type = 'send_message'"
)
SQLITE_CREATE = (
    f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
    "ON actions (tenant_id, JSON_EXTRACT(payload, '$.\"group_id\"'), id) "
    "WHERE action_type = 'send_message'"
)


def upgrade() -> None:
    _require_actions_table()
    if INDEX_NAME not in _index_names():
        _execute_ddl(POSTGRES_CREATE if _is_postgres() else SQLITE_CREATE)


def downgrade() -> None:
    _require_actions_table()
    if INDEX_NAME in _index_names(valid_only=False):
        _execute_ddl(f"DROP INDEX {'CONCURRENTLY ' if _is_postgres() else ''}{INDEX_NAME}")


def _execute_ddl(statement: str) -> None:
    if not _is_postgres():
        op.execute(statement)
        return
    with op.get_context().autocommit_block():
        op.execute(statement)


def _require_actions_table() -> None:
    if TABLE_NAME not in sa.inspect(op.get_bind()).get_table_names():
        raise RuntimeError("required table missing: actions")


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
