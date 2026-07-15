"""index incremental AI message-memory refreshes

Revision ID: 0102_ai_memory_updated_index
Revises: 0101_membership_latest_index
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0102_ai_memory_updated_index"
down_revision = "0101_membership_latest_index"
branch_labels = None
depends_on = None

TABLE_NAME = "ai_group_message_memory"
INDEX_NAME = "ix_ai_group_message_memory_tenant_status_updated"
POSTGRES_CREATE = (
    f"CREATE INDEX CONCURRENTLY {INDEX_NAME} ON {TABLE_NAME} "
    "(tenant_id, updated_at DESC) "
    "INCLUDE (status, planned_at, id, normalized_text, raw_text)"
)


def upgrade() -> None:
    _require_table()
    if INDEX_NAME in _index_names():
        return
    if _is_postgres():
        with op.get_context().autocommit_block():
            op.execute(POSTGRES_CREATE)
        return
    op.create_index(INDEX_NAME, TABLE_NAME, ["tenant_id", "updated_at"])


def downgrade() -> None:
    _require_table()
    if INDEX_NAME not in _index_names(valid_only=False):
        return
    if _is_postgres():
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY {INDEX_NAME}")
        return
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)


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
