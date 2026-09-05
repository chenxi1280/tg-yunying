"""Index account-scoped legacy call inventory without changing execution evidence."""
from alembic import op
import sqlalchemy as sa


revision = "0224_legacy_account_occupancy"
down_revision = "0223_burst_negative_outcome"
branch_labels = None
depends_on = None

TABLE_NAME = "execution_attempts"
INDEX_NAME = "ix_execution_attempts_account_usage"
INDEX_BODY = (
    f"{INDEX_NAME} ON {TABLE_NAME} (tenant_id, account_id, gateway_call_started_at) "
    "WHERE gateway_call_started_at IS NOT NULL OR status IN ('success','result_unknown')"
)


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {INDEX_BODY}"))
        return
    valid = _postgres_index_state()
    if valid is True:
        return
    with op.get_context().autocommit_block():
        if valid is False:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY {INDEX_NAME}"))
        op.execute(sa.text(f"CREATE INDEX CONCURRENTLY {INDEX_BODY}"))


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        op.execute(sa.text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
        return
    if _postgres_index_state() is None:
        return
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY {INDEX_NAME}"))


def _postgres_index_state():
    return op.get_bind().execute(sa.text("""
        SELECT i.indisvalid
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE c.relname = :index AND t.relname = :table AND n.nspname = current_schema()
    """), {"index": INDEX_NAME, "table": TABLE_NAME}).scalar_one_or_none()
