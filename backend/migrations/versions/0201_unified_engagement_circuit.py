"""Add unified engagement execution circuit state.

Revision ID: 0201_unified_engagement_circuit
Revises: 0200_engagement_proxy_bulkhead
"""

from alembic import op
import sqlalchemy as sa


revision = "0201_unified_engagement_circuit"
down_revision = "0200_engagement_proxy_bulkhead"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "execution_circuit_states"
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resilience_policy_revision_id", sa.String(36), sa.ForeignKey("execution_resilience_policy_revisions.id"), nullable=False),
        sa.Column("domain_kind", sa.String(32), nullable=False),
        sa.Column("domain_key", sa.String(120), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="closed"),
        sa.Column("failure_times", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("opened_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_code", sa.String(80), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "domain_kind", "domain_key", name="uq_execution_circuit_domain"),
    )
    op.create_index(
        "ix_execution_circuit_open",
        table,
        ["tenant_id", "state", "opened_until"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_circuit_open",
        table_name="execution_circuit_states",
    )
    op.drop_table("execution_circuit_states")
