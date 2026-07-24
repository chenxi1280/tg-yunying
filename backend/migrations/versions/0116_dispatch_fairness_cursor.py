"""Persist tenant-level dispatch fairness state.

Revision ID: 0116_dispatch_fairness_cursor
Revises: 0115_hh_credit_attempt_uuid
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0116_dispatch_fairness_cursor"
down_revision = "0115_hh_credit_attempt_uuid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dispatch_fairness_cursors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("last_claim_class", sa.String(length=40), server_default="", nullable=False),
        sa.Column("last_reason", sa.String(length=80), server_default="", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", name="uq_dispatch_fairness_cursor_tenant"),
    )
    op.create_index("ix_dispatch_fairness_cursor_updated", "dispatch_fairness_cursors", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_dispatch_fairness_cursor_updated", table_name="dispatch_fairness_cursors")
    op.drop_table("dispatch_fairness_cursors")
