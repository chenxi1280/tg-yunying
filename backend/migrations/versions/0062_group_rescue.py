"""group rescue settings and admission state

Revision ID: 0062_group_rescue
Revises: 0061_admission_delete_action
Create Date: 2026-06-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0062_group_rescue"
down_revision = "0061_admission_delete_action"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(table_name):
        return False
    return any(column["name"] == column_name for column in sa.inspect(bind).get_columns(table_name))


def upgrade() -> None:
    if not _has_column("tenants", "group_rescue_enabled"):
        op.add_column("tenants", sa.Column("group_rescue_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    if not _has_column("tenants", "group_rescue_admin_account_id"):
        op.add_column("tenants", sa.Column("group_rescue_admin_account_id", sa.Integer(), nullable=True))
        op.create_foreign_key("fk_tenants_group_rescue_admin_account", "tenants", "tg_accounts", ["group_rescue_admin_account_id"], ["id"])
    if not _has_column("tenants", "group_rescue_bot_username"):
        op.add_column("tenants", sa.Column("group_rescue_bot_username", sa.String(length=120), nullable=False, server_default=""))
    if not _has_column("task_membership_admission_items", "permission_failure_count"):
        op.add_column("task_membership_admission_items", sa.Column("permission_failure_count", sa.Integer(), nullable=False, server_default="0"))
    if not _has_column("task_membership_admission_items", "rescue_action_id"):
        op.add_column("task_membership_admission_items", sa.Column("rescue_action_id", sa.String(length=36), nullable=True))
        op.create_foreign_key("fk_membership_admission_rescue_action", "task_membership_admission_items", "actions", ["rescue_action_id"], ["id"])
    if not _has_column("task_membership_admission_items", "rescue_status"):
        op.add_column("task_membership_admission_items", sa.Column("rescue_status", sa.String(length=40), nullable=False, server_default=""))
    if not _has_column("task_membership_admission_items", "rescue_failure_detail"):
        op.add_column("task_membership_admission_items", sa.Column("rescue_failure_detail", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    for name in ("rescue_failure_detail", "rescue_status"):
        if _has_column("task_membership_admission_items", name):
            op.drop_column("task_membership_admission_items", name)
    if _has_column("task_membership_admission_items", "rescue_action_id"):
        op.drop_constraint("fk_membership_admission_rescue_action", "task_membership_admission_items", type_="foreignkey")
        op.drop_column("task_membership_admission_items", "rescue_action_id")
    if _has_column("task_membership_admission_items", "permission_failure_count"):
        op.drop_column("task_membership_admission_items", "permission_failure_count")
    if _has_column("tenants", "group_rescue_bot_username"):
        op.drop_column("tenants", "group_rescue_bot_username")
    if _has_column("tenants", "group_rescue_admin_account_id"):
        op.drop_constraint("fk_tenants_group_rescue_admin_account", "tenants", type_="foreignkey")
        op.drop_column("tenants", "group_rescue_admin_account_id")
    if _has_column("tenants", "group_rescue_enabled"):
        op.drop_column("tenants", "group_rescue_enabled")
