"""Add explicit rollout state for target gate and continuity ledger.

Revision ID: 0117_ai_continuity_rollout
Revises: 0116_dispatch_fairness_cursor
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0117_ai_continuity_rollout"
down_revision = "0116_dispatch_fairness_cursor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduling_settings",
        sa.Column("outbound_target_gate_mode", sa.String(length=20), server_default="dual_read", nullable=False),
    )
    op.add_column(
        "scheduling_settings",
        sa.Column("ai_group_send_continuity_v1", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "scheduling_settings",
        sa.Column("ai_group_continuity_release_anchor", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("scheduling_settings") as batch:
        batch.drop_column("ai_group_continuity_release_anchor")
        batch.drop_column("ai_group_send_continuity_v1")
        batch.drop_column("outbound_target_gate_mode")
