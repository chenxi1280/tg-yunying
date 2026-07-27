"""bind AI message memory to account mask evidence

Revision ID: 0129_ai_memory_account_mask
Revises: 0128_ai_group_daily_targets
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0129_ai_memory_account_mask"
down_revision = "0128_ai_group_daily_targets"
branch_labels = None
depends_on = None

TABLE = "ai_group_message_memory"
INDEX = "ix_ai_group_message_memory_account_window"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("account_mask_id", sa.String(36), nullable=False, server_default=""))
    op.add_column(TABLE, sa.Column("account_mask_version", sa.Integer(), nullable=True))
    op.add_column(TABLE, sa.Column("mask_contract_version", sa.String(40), nullable=False, server_default=""))
    op.add_column(TABLE, sa.Column("mask_snapshot_hash", sa.String(64), nullable=False, server_default=""))
    op.add_column(TABLE, sa.Column("mask_status", sa.String(30), nullable=False, server_default=""))
    op.add_column(TABLE, sa.Column("content_source", sa.String(40), nullable=False, server_default=""))
    op.create_index(INDEX, TABLE, ["tenant_id", "account_id", "status", "planned_at"])


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_column(TABLE, "content_source")
    op.drop_column(TABLE, "mask_status")
    op.drop_column(TABLE, "mask_snapshot_hash")
    op.drop_column(TABLE, "mask_contract_version")
    op.drop_column(TABLE, "account_mask_version")
    op.drop_column(TABLE, "account_mask_id")
