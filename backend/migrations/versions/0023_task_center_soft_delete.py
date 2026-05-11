"""task center soft delete fields

Revision ID: 0023_task_center_soft_delete
Revises: 0022_task_center_5_types
Create Date: 2026-05-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0023_task_center_soft_delete"
down_revision = "0022_task_center_5_types"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_names(table: str) -> set[str]:
    if not _table_exists(table):
        return set()
    return {item["name"] for item in sa.inspect(_bind()).get_columns(table)}


def upgrade() -> None:
    columns = _column_names("tasks")
    if not columns:
        return
    if "deleted_at" not in columns:
        op.add_column("tasks", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    if "deleted_by" not in columns:
        op.add_column("tasks", sa.Column("deleted_by", sa.String(length=100), nullable=False, server_default=""))
    if "delete_reason" not in columns:
        op.add_column("tasks", sa.Column("delete_reason", sa.String(length=255), nullable=False, server_default=""))


def downgrade() -> None:
    columns = _column_names("tasks")
    for column in ["delete_reason", "deleted_by", "deleted_at"]:
        if column in columns:
            op.drop_column("tasks", column)
