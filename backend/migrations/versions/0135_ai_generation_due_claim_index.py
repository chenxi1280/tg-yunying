"""Index AI generation due claims.

Revision ID: 0135_ai_generation_due_claim
Revises: 0134_shared_dispatch
Create Date: 2026-08-03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0135_ai_generation_due_claim"
down_revision = "0134_shared_dispatch"
branch_labels = None
depends_on = None

TABLE_NAME = "actions"
INDEX_NAME = "ix_actions_ai_generation_due_claim"
POSTGRES_CREATE = (
    f"CREATE INDEX CONCURRENTLY {INDEX_NAME} "
    "ON actions (scheduled_at, created_at, id) "
    "WHERE task_type = 'group_ai_chat' "
    "AND action_type = 'send_message' "
    "AND status = 'pending' "
    "AND account_id IS NOT NULL "
    "AND CAST(payload ->> 'ai_generation_status' AS VARCHAR) "
    "IN ('pending', 'ai_result_persist_unknown') "
    "AND COALESCE(CAST(payload ->> 'message_text' AS VARCHAR), '') = ''"
)
SQLITE_CREATE = (
    f"CREATE INDEX {INDEX_NAME} "
    "ON actions (scheduled_at, created_at, id) "
    "WHERE task_type = 'group_ai_chat' "
    "AND action_type = 'send_message' "
    "AND status = 'pending' "
    "AND account_id IS NOT NULL "
    "AND CAST(JSON_EXTRACT(payload, '$.\"ai_generation_status\"') AS VARCHAR) "
    "IN ('pending', 'ai_result_persist_unknown') "
    "AND COALESCE(CAST(JSON_EXTRACT(payload, '$.\"message_text\"') AS VARCHAR), '') = ''"
)


def upgrade() -> None:
    _require_actions_table()
    valid_indexes = _index_names()
    if INDEX_NAME in valid_indexes:
        return
    if INDEX_NAME in _index_names(valid_only=False):
        raise RuntimeError(f"invalid index exists: {INDEX_NAME}")
    _execute_ddl(POSTGRES_CREATE if _is_postgres() else SQLITE_CREATE)


def downgrade() -> None:
    _require_actions_table()
    if INDEX_NAME not in _index_names(valid_only=False):
        return
    concurrent = "CONCURRENTLY " if _is_postgres() else ""
    _execute_ddl(f"DROP INDEX {concurrent}{INDEX_NAME}")


def _execute_ddl(statement: str) -> None:
    if not _is_postgres():
        op.execute(statement)
        return
    with op.get_context().autocommit_block():
        op.execute(statement)


def _require_actions_table() -> None:
    if TABLE_NAME not in sa.inspect(op.get_bind()).get_table_names():
        raise RuntimeError(f"required table missing: {TABLE_NAME}")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _index_names(*, valid_only: bool = True) -> set[str]:
    bind = op.get_bind()
    if not _is_postgres():
        return {item["name"] for item in sa.inspect(bind).get_indexes(TABLE_NAME)}
    validity_filter = "AND index_meta.indisvalid" if valid_only else ""
    statement = sa.text(
        "SELECT index_class.relname FROM pg_index AS index_meta "
        "JOIN pg_class AS table_class ON table_class.oid = index_meta.indrelid "
        "JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid "
        "JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace "
        "WHERE table_class.relname = :table_name "
        "AND namespace.nspname = current_schema() "
        f"{validity_filter}"
    )
    return set(bind.execute(statement, {"table_name": TABLE_NAME}).scalars())
