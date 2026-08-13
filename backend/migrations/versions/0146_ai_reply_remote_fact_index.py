"""Index authoritative AI reply remote message lookup."""

from alembic import op
import sqlalchemy as sa


revision = "0146_ai_reply_remote_fact_index"
down_revision = "0145_channel_view_daily_targets"
branch_labels = None
depends_on = None


TABLE_NAME = "execution_attempts"
INDEX_NAME = "ix_execution_attempts_success_remote"
INDEX_COLUMNS = "remote_message_id, action_id, attempt_no DESC"
PREDICATE = "status = 'success' AND remote_message_id <> ''"
POSTGRES_CREATE = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
    f"ON {TABLE_NAME} ({INDEX_COLUMNS}) WHERE {PREDICATE}"
)
SQLITE_CREATE = (
    f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
    f"ON {TABLE_NAME} ({INDEX_COLUMNS}) WHERE {PREDICATE}"
)


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
