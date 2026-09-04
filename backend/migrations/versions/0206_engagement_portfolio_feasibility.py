"""Add unified engagement portfolio feasibility plans.

Revision ID: 0206_engagement_portfolio
Revises: 0205_engagement_health_probe
"""

from alembic import op
import sqlalchemy as sa


revision = "0206_engagement_portfolio"
down_revision = "0205_engagement_health_probe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portfolio_feasibility_plan_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("planning_horizon", sa.String(100), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("trigger_task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trigger_kind", sa.String(48), nullable=False),
        sa.Column("trigger_identity", sa.String(240), nullable=False),
        sa.Column("task_set_hash", sa.String(64), nullable=False),
        sa.Column("policy_revision_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("demand_snapshot", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("account_task_day_load", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("deficits", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "planning_horizon", "trigger_kind", "trigger_identity", "input_hash", name="uq_portfolio_feasibility_input"),
    )
    op.create_index(
        "ix_portfolio_feasibility_horizon",
        "portfolio_feasibility_plan_revisions",
        ["tenant_id", "planning_horizon", "decision"],
    )
    op.create_table(
        "account_portfolio_load_reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_day_ledger_id", sa.String(36), sa.ForeignKey("task_day_ledgers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("portfolio_plan_id", sa.String(36), sa.ForeignKey("portfolio_feasibility_plan_revisions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_day", sa.Date(), nullable=False),
        sa.Column("action_class", sa.String(40), nullable=False),
        sa.Column("demand_identity", sa.String(240), nullable=False),
        sa.Column("demand_hash", sa.String(64), nullable=False),
        sa.Column("reserved_units", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "task_day", "task_id", "action_class", "demand_identity", "account_id", name="uq_account_portfolio_demand_account"),
    )
    op.create_index(
        "ix_account_portfolio_capacity",
        "account_portfolio_load_reservations",
        ["tenant_id", "task_day", "account_id", "action_class", "state"],
    )
    op.create_index(
        "ix_account_portfolio_task",
        "account_portfolio_load_reservations",
        ["task_id", "task_day_ledger_id", "action_class", "state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_account_portfolio_task",
        table_name="account_portfolio_load_reservations",
    )
    op.drop_index(
        "ix_account_portfolio_capacity",
        table_name="account_portfolio_load_reservations",
    )
    op.drop_table("account_portfolio_load_reservations")
    op.drop_index(
        "ix_portfolio_feasibility_horizon",
        table_name="portfolio_feasibility_plan_revisions",
    )
    op.drop_table("portfolio_feasibility_plan_revisions")
