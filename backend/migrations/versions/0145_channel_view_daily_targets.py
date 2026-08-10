"""add channel view daily message targets

Revision ID: 0145_channel_view_daily_targets
Revises: 0144_avatar_material_sources
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0145_channel_view_daily_targets"
down_revision = "0144_avatar_material_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "channel_view_daily_message_targets" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "channel_view_daily_message_targets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("task_day_ledger_id", sa.String(length=36), nullable=False),
        sa.Column("target_peer_id", sa.String(length=120), nullable=False),
        sa.Column("channel_message_id", sa.Integer(), nullable=False),
        sa.Column("target_revision", sa.Integer(), nullable=False),
        sa.Column("daily_target_snapshot", sa.Integer(), nullable=False),
        sa.Column("total_target_snapshot", sa.Integer(), nullable=False),
        sa.Column("lifetime_confirmed_at_attach", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ledger_confirmed_at_attach", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("effective_target_snapshot", sa.Integer(), nullable=False),
        sa.Column("accrual_anchor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_state", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_day_ledger_id"], ["task_day_ledgers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["channel_message_id"], ["channel_messages.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "task_day_ledger_id",
            "target_peer_id",
            "channel_message_id",
            name="uq_channel_view_daily_message_target",
        ),
    )
    op.create_index(
        "ix_channel_view_daily_target_ledger",
        "channel_view_daily_message_targets",
        ["task_day_ledger_id", "id"],
    )


def downgrade() -> None:
    if "channel_view_daily_message_targets" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index(
        "ix_channel_view_daily_target_ledger",
        table_name="channel_view_daily_message_targets",
    )
    op.drop_table("channel_view_daily_message_targets")
