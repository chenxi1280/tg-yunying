"""Add cross-adapter source journey ownership.

Revision ID: 0210_cross_adapter_journey
Revises: 0209_comment_peer_identity
"""

from alembic import op
import sqlalchemy as sa


revision = "0210_cross_adapter_journey"
down_revision = "0209_comment_peer_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_journey_plans()
    _create_journey_plan_indexes()
    _create_journey_decisions()


def _create_journey_plans() -> None:
    table = "cross_adapter_source_journey_plan_revisions"
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_revision_id",
            sa.String(36),
            sa.ForeignKey("channel_message_source_revisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_day", sa.Date(), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_task_set_hash", sa.String(64), nullable=False),
        sa.Column("policy_revision", sa.String(64), nullable=False),
        sa.Column("adapter_constraints", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("hard_constraint_hash", sa.String(64), nullable=False),
        sa.Column("objective_policy", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("edge_set", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("edge_set_hash", sa.String(64), nullable=False),
        sa.Column("overlap_metrics", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("deficits", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column(
            "supersedes_plan_id",
            sa.String(36),
            sa.ForeignKey(f"{table}.id"),
            nullable=True,
        ),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "source_revision_id",
            "task_day",
            "plan_revision",
            name="uq_source_journey_plan_revision",
        ),
    )


def _create_journey_plan_indexes() -> None:
    op.create_index(
        "uq_source_journey_plan_active",
        "cross_adapter_source_journey_plan_revisions",
        ["tenant_id", "source_revision_id", "task_day"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )


def _create_journey_decisions() -> None:
    table = "source_journey_decisions"
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            sa.String(36),
            sa.ForeignKey(
                "cross_adapter_source_journey_plan_revisions.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action_class", sa.String(40), nullable=False),
        sa.Column("journey_class", sa.String(40), nullable=False),
        sa.Column("decision_state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "plan_id",
            "task_id",
            "action_class",
            "account_id",
            name="uq_source_journey_decision_edge",
        ),
    )
    op.create_index(
        "ix_source_journey_decision_task",
        table,
        ["task_id", "action_class", "decision_state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_source_journey_decision_task", table_name="source_journey_decisions"
    )
    op.drop_table("source_journey_decisions")
    op.drop_index(
        "uq_source_journey_plan_active",
        table_name="cross_adapter_source_journey_plan_revisions",
    )
    op.drop_table("cross_adapter_source_journey_plan_revisions")
