"""Separate remote business outcome from runner termination proof.

Revision ID: 0212_remote_fence_transport_ack
Revises: 0211_account_fleet_activity
"""

from alembic import op
import sqlalchemy as sa


revision = "0212_remote_fence_transport_ack"
down_revision = "0211_account_fleet_activity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "remote_invocation_fences"
    op.add_column(
        table,
        sa.Column("runner_generation", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        table,
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        table,
        sa.Column(
            "transport_termination_state",
            sa.String(40),
            nullable=False,
            server_default="unproven",
        ),
    )
    op.execute(sa.text(
        "UPDATE remote_invocation_fences "
        "SET transport_termination_state = CASE "
        "WHEN state = 'terminal' AND transport_terminated_at IS NOT NULL "
        "THEN 'legacy_terminal' ELSE 'unproven' END"
    ))


def downgrade() -> None:
    table = "remote_invocation_fences"
    op.drop_column(table, "transport_termination_state")
    op.drop_column(table, "cancellation_requested_at")
    op.drop_column(table, "runner_generation")
