"""Add same-adapter canonical target writer claims.

Revision ID: 0203_task_target_scope_claims
Revises: 0202_account_pacing_action_class
"""

from alembic import op
import sqlalchemy as sa


revision = "0203_task_target_scope_claims"
down_revision = "0202_account_pacing_action_class"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_target_scope_claims",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False),
        sa.Column("adapter_type", sa.String(40), nullable=False),
        sa.Column("canonical_peer_id", sa.String(120), nullable=False),
        sa.Column("target_kind", sa.String(24), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(80), nullable=False, server_default=""),
        sa.UniqueConstraint(
            "task_id",
            "task_lifecycle_epoch",
            "adapter_type",
            "canonical_peer_id",
            name="uq_task_target_scope_epoch",
        ),
    )
    op.create_index(
        "uq_task_target_scope_active_writer",
        "task_target_scope_claims",
        ["tenant_id", "adapter_type", "canonical_peer_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )
    op.create_index(
        "ix_task_target_scope_task_state",
        "task_target_scope_claims",
        ["task_id", "state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_target_scope_task_state", table_name="task_target_scope_claims"
    )
    op.drop_index(
        "uq_task_target_scope_active_writer", table_name="task_target_scope_claims"
    )
    op.drop_table("task_target_scope_claims")
