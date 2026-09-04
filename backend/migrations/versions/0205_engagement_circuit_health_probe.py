"""Add independent unified engagement circuit health probes.

Revision ID: 0205_engagement_health_probe
Revises: 0204_engagement_conversation
"""

from alembic import op
import sqlalchemy as sa


revision = "0205_engagement_health_probe"
down_revision = "0204_engagement_conversation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_circuit_states",
        sa.Column("probe_attempt_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "execution_circuit_states",
        sa.Column("probe_lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "health_probe_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("circuit_state_id", sa.String(36), sa.ForeignKey("execution_circuit_states.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resilience_policy_revision_id", sa.String(36), sa.ForeignKey("execution_resilience_policy_revisions.id"), nullable=False),
        sa.Column("circuit_version", sa.Integer(), nullable=False),
        sa.Column("probe_revision", sa.Integer(), nullable=False),
        sa.Column("domain_kind", sa.String(32), nullable=False),
        sa.Column("domain_key", sa.String(120), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
        sa.Column("dependency_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("owner_token", sa.String(80), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="claimed"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome_code", sa.String(80), nullable=False, server_default=""),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("circuit_state_id", "probe_revision", name="uq_health_probe_circuit_revision"),
    )
    op.create_index(
        "uq_health_probe_circuit_active",
        "health_probe_attempts",
        ["circuit_state_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('claimed', 'running')"),
        sqlite_where=sa.text("state IN ('claimed', 'running')"),
    )
    op.create_index(
        "ix_health_probe_due",
        "health_probe_attempts",
        ["state", "deadline_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_health_probe_due", table_name="health_probe_attempts")
    op.drop_index(
        "uq_health_probe_circuit_active",
        table_name="health_probe_attempts",
    )
    op.drop_table("health_probe_attempts")
    op.drop_column("execution_circuit_states", "probe_lease_until")
    op.drop_column("execution_circuit_states", "probe_attempt_id")
