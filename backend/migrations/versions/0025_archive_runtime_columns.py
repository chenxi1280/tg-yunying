"""archive runtime columns

Revision ID: 0025_archive_runtime_columns
Revises: 0024_rule_set_versions
Create Date: 2026-05-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0025_archive_runtime_columns"
down_revision = "0024_rule_set_versions"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_exists(table: str, column: str) -> bool:
    if not _table_exists(table):
        return False
    return column in {item["name"] for item in sa.inspect(_bind()).get_columns(table)}


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if _table_exists(table) and not _column_exists(table, column.name):
        op.add_column(table, column)


def upgrade() -> None:
    _add_column_if_missing("group_archives", sa.Column("sync_mode", sa.String(length=30), nullable=False, server_default="sync"))
    _add_column_if_missing("group_archives", sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""))
    _add_column_if_missing("group_archives", sa.Column("summary", sa.Text(), nullable=False, server_default=""))
    _add_column_if_missing("group_archives", sa.Column("new_group_plan", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    for name in ["new_group_plan", "summary", "failure_detail", "sync_mode"]:
        if _column_exists("group_archives", name):
            op.drop_column("group_archives", name)
