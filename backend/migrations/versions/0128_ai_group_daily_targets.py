"""add durable AI group daily target ledger

Revision ID: 0128_ai_group_daily_targets
Revises: 0127_group_send_claim_slot
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0128_ai_group_daily_targets"
down_revision = "0127_group_send_claim_slot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_group_daily_targets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("tg_groups.id"), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("configured_message_target", sa.Integer(), nullable=False),
        sa.Column("frozen_account_count", sa.Integer(), nullable=False),
        sa.Column("effective_message_target", sa.Integer(), nullable=False),
        sa.Column("daily_fulfillment_phase", sa.String(32), nullable=False),
        sa.Column("scope_frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("full_day_committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmed_message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("coverage_confirmed_account_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "task_id",
            "group_id",
            "target_date",
            name="uq_task_group_daily_target",
        ),
    )
    op.create_index(
        "ix_task_group_daily_target_task_date",
        "task_group_daily_targets",
        ["task_id", "target_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_group_daily_target_task_date", table_name="task_group_daily_targets")
    op.drop_table("task_group_daily_targets")
