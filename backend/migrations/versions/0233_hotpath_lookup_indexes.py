"""Index all-status remote ownership and ordered per-group memory history."""
from alembic import op
import sqlalchemy as sa

revision = "0233_hotpath_lookup_indexes"
down_revision = "0232_admission_evidence"
branch_labels = None
depends_on = None
INDEXES = (
    ("ix_execution_attempts_remote_identity", "execution_attempts", "remote_message_id, action_id"),
    ("ix_ai_group_memory_group_recent", "ai_group_message_memory", "tenant_id, group_id, planned_at DESC"),
)


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    for name, table, columns in INDEXES:
        if not postgres:
            op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})"))
            continue
        if _existing_index_valid(name, table=table, columns=columns):
            continue
        with op.get_context().autocommit_block():
            op.execute(sa.text(f"CREATE INDEX CONCURRENTLY {name} ON {table} ({columns})"))


def _existing_index_valid(name, *, table, columns):
    row = op.get_bind().execute(sa.text("""
        SELECT i.indisvalid, i.indisready, pg_get_indexdef(c.oid) AS definition
        FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relname=:name AND n.nspname=current_schema()
    """), {"name": name}).mappings().one_or_none()
    if row is None:
        return False
    expected = f"CREATE INDEX {name} ON public.{table} USING btree ({columns})"
    if not row["indisvalid"] or not row["indisready"] or row["definition"] != expected:
        raise RuntimeError(f"hotpath_index_state_or_definition_mismatch:{name}")
    return True


def downgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    for name, _table, _columns in reversed(INDEXES):
        if postgres:
            with op.get_context().autocommit_block():
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
        else:
            op.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
