"""Index AI surface history and complete group observations without changing facts."""
from alembic import op
import sqlalchemy as sa


revision = "0227_ai_group_history_indexes"
down_revision = "0226_task_retirement"
branch_labels = None
depends_on = None
INDEXES = (
    ("ix_actions_ai_surface_history", "actions"),
    ("ix_group_context_messages_observation_recent", "group_context_messages"),
)


def _columns(index_name: str, postgres: bool) -> str:
    if index_name == "ix_actions_ai_surface_history":
        surface = (
            "CAST((payload ->> 'surface_scope_key') AS VARCHAR)"
            if postgres else "JSON_EXTRACT(payload, '$.surface_scope_key')"
        )
        return f"tenant_id, task_type, ({surface})"
    return "tenant_id, group_id, coalesce(sent_at, created_at) DESC, id DESC"


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    for index_name, table_name in INDEXES:
        columns = _columns(index_name, postgres)
        if not postgres:
            op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} ({columns})"))
            continue
        if _index_state(index_name, table_name) is True:
            continue
        with op.get_context().autocommit_block():
            if _index_state(index_name, table_name) is False:
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY {index_name}"))
            op.execute(sa.text(f"CREATE INDEX CONCURRENTLY {index_name} ON {table_name} ({columns})"))


def downgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    for index_name, _ in reversed(INDEXES):
        if postgres:
            with op.get_context().autocommit_block():
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}"))
        else:
            op.execute(sa.text(f"DROP INDEX IF EXISTS {index_name}"))


def _index_state(index_name: str, table_name: str) -> bool | None:
    return op.get_bind().execute(sa.text("""
        SELECT index_meta.indisvalid
        FROM pg_index AS index_meta
        JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid
        JOIN pg_class AS table_class ON table_class.oid = index_meta.indrelid
        JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
        WHERE index_class.relname = :index_name
          AND table_class.relname = :table_name
          AND namespace.nspname = current_schema()
    """), {"index_name": index_name, "table_name": table_name}).scalar_one_or_none()
