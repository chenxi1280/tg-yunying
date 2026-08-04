"""Index the AI generation recent-context query."""

from alembic import op
import sqlalchemy as sa


revision = "0142_ai_context_recent_index"
down_revision = "0141_single_active_ai_provider"
branch_labels = None
depends_on = None


TABLE_NAME = "group_context_messages"
INDEX_NAME = "ix_group_context_messages_ai_recent"
POSTGRES_CREATE = f"""
CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME}
ON {TABLE_NAME} (
    tenant_id,
    group_id,
    coalesce(sent_at, created_at) DESC,
    id DESC
)
WHERE is_bot IS false AND content <> ''
"""
SQLITE_CREATE = f"""
CREATE INDEX IF NOT EXISTS {INDEX_NAME}
ON {TABLE_NAME} (
    tenant_id,
    group_id,
    coalesce(sent_at, created_at) DESC,
    id DESC
)
WHERE is_bot = 0 AND content <> ''
"""


def upgrade() -> None:
    _require_table()
    if not _is_postgres():
        op.execute(sa.text(SQLITE_CREATE))
        return
    if _postgres_index_valid():
        return
    with op.get_context().autocommit_block():
        if _postgres_index_exists():
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY {INDEX_NAME}"))
        op.execute(sa.text(POSTGRES_CREATE))


def downgrade() -> None:
    _require_table()
    if not _is_postgres():
        op.execute(sa.text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
        return
    if not _postgres_index_exists():
        return
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY {INDEX_NAME}"))


def _require_table() -> None:
    if TABLE_NAME not in sa.inspect(op.get_bind()).get_table_names():
        raise RuntimeError(f"required table missing: {TABLE_NAME}")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _postgres_index_exists() -> bool:
    return _postgres_index_state() is not None


def _postgres_index_valid() -> bool:
    return _postgres_index_state() is True


def _postgres_index_state() -> bool | None:
    row = op.get_bind().execute(sa.text("""
        SELECT index_meta.indisvalid
        FROM pg_index AS index_meta
        JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid
        JOIN pg_class AS table_class ON table_class.oid = index_meta.indrelid
        JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
        WHERE index_class.relname = :index_name
          AND table_class.relname = :table_name
          AND namespace.nspname = current_schema()
    """), {"index_name": INDEX_NAME, "table_name": TABLE_NAME}).scalar_one_or_none()
    return bool(row) if row is not None else None
