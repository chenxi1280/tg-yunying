"""persist hard-hourly planner checkpoints

Revision ID: 0105_hard_hourly_scheduler_checkpoint
Revises: 0104_cpu_backpressure_indexes
Create Date: 2026-07-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0105_hard_hourly_checkpoint"
down_revision = "0104_cpu_backpressure_indexes"
branch_labels = None
depends_on = None

TABLE_NAME = "tasks"
COLUMN_NAME = "hard_hourly_next_check_at"
INDEX_NAME = "ix_tasks_hard_hourly_wake"
POSTGRES_CREATE_INDEX = (
    f"CREATE INDEX CONCURRENTLY {INDEX_NAME} ON {TABLE_NAME} "
    "(hard_hourly_next_check_at, priority, next_run_at, created_at) "
    "WHERE status = 'running' AND type = 'group_ai_chat' AND deleted_at IS NULL"
)


def upgrade() -> None:
    _require_table()
    if COLUMN_NAME not in _column_names():
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.DateTime(timezone=True), nullable=True))
    if INDEX_NAME in _index_names():
        return
    if _is_postgres():
        with op.get_context().autocommit_block():
            op.execute(POSTGRES_CREATE_INDEX)
        return
    op.create_index(INDEX_NAME, TABLE_NAME, [COLUMN_NAME, "priority", "next_run_at", "created_at"])


def downgrade() -> None:
    _require_table()
    if INDEX_NAME in _index_names(valid_only=False):
        if _is_postgres():
            with op.get_context().autocommit_block():
                op.execute(f"DROP INDEX CONCURRENTLY {INDEX_NAME}")
        else:
            op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    if COLUMN_NAME in _column_names():
        op.drop_column(TABLE_NAME, COLUMN_NAME)


def _require_table() -> None:
    if TABLE_NAME not in sa.inspect(op.get_bind()).get_table_names():
        raise RuntimeError(f"required table missing: {TABLE_NAME}")


def _column_names() -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(TABLE_NAME)}


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
