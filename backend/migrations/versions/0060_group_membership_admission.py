"""group membership admission task items

Revision ID: 0060_group_membership_admission
Revises: 0059_ai_group_hard_target_60
Create Date: 2026-06-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0060_group_membership_admission"
down_revision = "0059_ai_group_hard_target_60"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("task_membership_admission_items"):
        return
    op.create_table(
        "task_membership_admission_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=False),
        sa.Column("phase", sa.String(length=40), nullable=False, server_default="pending"),
        sa.Column("membership_action_id", sa.String(length=36), sa.ForeignKey("actions.id"), nullable=True),
        sa.Column("test_message_action_id", sa.String(length=36), sa.ForeignKey("actions.id"), nullable=True),
        sa.Column("test_message_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("test_message_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("delete_after_send", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("delete_status", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("failure_type", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("manual_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "account_id", name="uq_membership_admission_task_account"),
    )
    op.create_index("ix_membership_admission_task_phase", "task_membership_admission_items", ["task_id", "phase"])
    op.create_index("ix_membership_admission_manual", "task_membership_admission_items", ["task_id", "manual_required"])


def downgrade() -> None:
    if not _has_table("task_membership_admission_items"):
        return
    op.drop_index("ix_membership_admission_manual", table_name="task_membership_admission_items")
    op.drop_index("ix_membership_admission_task_phase", table_name="task_membership_admission_items")
    op.drop_table("task_membership_admission_items")
