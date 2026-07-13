"""index the tenant-wide AI message-memory dedupe window

Revision ID: 0091_ai_memory_index
Revises: 0090_ai_group_fallback
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0091_ai_memory_index"
down_revision = "0090_ai_group_fallback"
branch_labels = None
depends_on = None

TABLE_NAME = "ai_group_message_memory"
INDEX_NAME = "ix_ai_group_message_memory_tenant_status_planned"
INDEX_COLUMNS = ["tenant_id", "status", sa.text("planned_at DESC")]


def upgrade() -> None:
    if not _has_table() or INDEX_NAME in _index_names():
        return
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE INDEX CONCURRENTLY {INDEX_NAME} "
                f"ON {TABLE_NAME} (tenant_id, status, planned_at DESC)"
            )
        return
    op.create_index(INDEX_NAME, TABLE_NAME, INDEX_COLUMNS)


def downgrade() -> None:
    if not _has_table() or INDEX_NAME not in _index_names(valid_only=False):
        return
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY {INDEX_NAME}")
        return
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)


def _has_table() -> bool:
    return TABLE_NAME in sa.inspect(op.get_bind()).get_table_names()


def _index_names(*, valid_only: bool = True) -> set[str]:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
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
