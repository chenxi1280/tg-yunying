"""index action foreign keys used by runtime retention

Revision ID: 0095_retention_fk_indexes
Revises: 0094_channel_comment_history
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0095_retention_fk_indexes"
down_revision = "0094_channel_comment_history"
branch_labels = None
depends_on = None

INDEX_DEFINITIONS = (
    ("ix_review_queue_action_id", "review_queue", "action_id"),
    ("ix_task_daily_coverage_reserved_action", "task_account_daily_coverage", "reserved_action_id"),
    ("ix_task_daily_coverage_last_success_action", "task_account_daily_coverage", "last_success_action_id"),
    ("ix_membership_admission_membership_action", "task_membership_admission_items", "membership_action_id"),
    ("ix_membership_admission_test_message_action", "task_membership_admission_items", "test_message_action_id"),
    ("ix_membership_admission_delete_action", "task_membership_admission_items", "delete_action_id"),
    ("ix_membership_admission_rescue_action", "task_membership_admission_items", "rescue_action_id"),
)
REQUIRED_TABLES = {table for _name, table, _column in INDEX_DEFINITIONS}


def upgrade() -> None:
    _require_tables()
    for name, table, column in INDEX_DEFINITIONS:
        _create_index(name, table, column)


def downgrade() -> None:
    _require_tables()
    for name, table, _column in reversed(INDEX_DEFINITIONS):
        _drop_index(name, table)


def _create_index(name: str, table: str, column: str) -> None:
    if name in _index_names(table):
        return
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"CREATE INDEX CONCURRENTLY {name} ON {table} ({column})")
        return
    op.create_index(name, table, [column], unique=False)


def _drop_index(name: str, table: str) -> None:
    if name not in _index_names(table, valid_only=False):
        return
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY {name}")
        return
    op.drop_index(name, table_name=table)


def _require_tables() -> None:
    missing = REQUIRED_TABLES - set(sa.inspect(op.get_bind()).get_table_names())
    if missing:
        raise RuntimeError(f"required tables missing: {', '.join(sorted(missing))}")


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
