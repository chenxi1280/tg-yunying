"""Freeze action class on account pacing reservations.

Revision ID: 0202_account_pacing_action_class
Revises: 0201_unified_engagement_circuit
"""

from alembic import op
import sqlalchemy as sa


revision = "0202_account_pacing_action_class"
down_revision = "0201_unified_engagement_circuit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_pacing_reservations",
        sa.Column("action_class", sa.String(32), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("account_pacing_reservations", "action_class")
