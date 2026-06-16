"""membership admission delete action link

Revision ID: 0061_admission_delete_action
Revises: 0060_group_membership_admission
Create Date: 2026-06-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0061_admission_delete_action"
down_revision = "0060_group_membership_admission"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(table_name):
        return False
    return any(column["name"] == column_name for column in sa.inspect(bind).get_columns(table_name))


def upgrade() -> None:
    if _has_column("task_membership_admission_items", "delete_action_id"):
        return
    op.add_column("task_membership_admission_items", sa.Column("delete_action_id", sa.String(length=36), nullable=True))
    op.create_foreign_key(
        "fk_membership_admission_delete_action",
        "task_membership_admission_items",
        "actions",
        ["delete_action_id"],
        ["id"],
    )


def downgrade() -> None:
    if not _has_column("task_membership_admission_items", "delete_action_id"):
        return
    op.drop_constraint("fk_membership_admission_delete_action", "task_membership_admission_items", type_="foreignkey")
    op.drop_column("task_membership_admission_items", "delete_action_id")
