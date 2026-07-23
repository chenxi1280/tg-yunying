"""bound planner, recovery, and runtime statistics scans

Revision ID: 0093_runtime_stats_indexes
Revises: 0092_ai_memory_action_idx
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0093_runtime_stats_indexes"
down_revision = "0092_ai_memory_action_idx"
branch_labels = None
depends_on = None

CURSOR_TABLE = "task_daily_coverage_plan_cursors"
REQUIRED_TABLES = {"actions", "task_account_daily_coverage", "tasks", "tenants"}
INDEX_DEFINITIONS = (
    (
        "ix_actions_task_stats_reconcile",
        "actions",
        "tenant_id, task_id, status, action_type, account_id, executed_at",
        "",
    ),
    (
        "ix_actions_executing_recovery",
        "actions",
        "scheduled_at, lease_expires_at, task_id, id",
        " WHERE status = 'executing'",
    ),
    (
        "ix_task_daily_coverage_plan_ready",
        "task_account_daily_coverage",
        "task_id, coverage_date, state, targeted_at, account_id, id",
        "",
    ),
)


def upgrade() -> None:
    _require_tables()
    _create_cursor_table()
    for index_name, table_name, columns, where_clause in INDEX_DEFINITIONS:
        _create_index(index_name, table_name, columns, where_clause)


def downgrade() -> None:
    _require_tables()
    for index_name, table_name, _columns, _where_clause in reversed(INDEX_DEFINITIONS):
        _drop_index(index_name, table_name)
    if CURSOR_TABLE not in _table_names():
        raise RuntimeError(f"required table missing: {CURSOR_TABLE}")
    op.drop_table(CURSOR_TABLE)


def _create_cursor_table() -> None:
    if CURSOR_TABLE in _table_names():
        return
    op.create_table(
        CURSOR_TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("coverage_date", sa.Date(), nullable=False),
        sa.Column("last_targeted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_account_id", sa.Integer(), nullable=True),
        sa.Column("last_coverage_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("wrap_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "task_id",
            "coverage_date",
            name="uq_task_daily_coverage_plan_cursor",
        ),
    )


def _create_index(name: str, table: str, columns: str, where_clause: str) -> None:
    if name in _index_names(table):
        return
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"CREATE INDEX CONCURRENTLY {name} ON {table} ({columns}){where_clause}")
        return
    op.create_index(
        name,
        table,
        [column.strip() for column in columns.split(",")],
        unique=False,
        sqlite_where=sa.text(where_clause.removeprefix(" WHERE ")) if where_clause else None,
    )


def _drop_index(name: str, table: str) -> None:
    if name not in _index_names(table, valid_only=False):
        return
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY {name}")
        return
    op.drop_index(name, table_name=table)


def _require_tables() -> None:
    missing = REQUIRED_TABLES - _table_names()
    if missing:
        raise RuntimeError(f"required tables missing: {', '.join(sorted(missing))}")


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names(table: str, *, valid_only: bool = True) -> set[str]:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return {item["name"] for item in sa.inspect(bind).get_indexes(table)}
    validity_filter = "AND index_meta.indisvalid" if valid_only else ""
    query = sa.text(
        "SELECT index_class.relname FROM pg_index AS index_meta "
        "JOIN pg_class AS table_class ON table_class.oid = index_meta.indrelid "
        "JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid "
        "JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace "
        "WHERE table_class.relname = :table_name AND namespace.nspname = current_schema() "
        f"{validity_filter}"
    )
    return set(bind.execute(query, {"table_name": table}).scalars())
