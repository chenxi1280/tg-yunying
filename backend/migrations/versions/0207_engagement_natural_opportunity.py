"""Add natural opportunity and managed presence plans.

Revision ID: 0207_engagement_opportunity
Revises: 0206_engagement_portfolio
"""

from alembic import op
import sqlalchemy as sa


revision = "0207_engagement_opportunity"
down_revision = "0206_engagement_portfolio"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_presence_policy_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_consecutive_system_turns", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("absolute_daily_authored_cap", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("managed_to_external_ratio_bps", sa.Integer(), nullable=False, server_default="10000"),
        sa.Column("bootstrap_allowance", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "revision", name="uq_managed_presence_policy_revision"),
    )
    op.create_index(
        "uq_managed_presence_policy_active",
        "managed_presence_policy_revisions",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )
    op.create_table(
        "managed_presence_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_day_ledger_id", sa.String(36), sa.ForeignKey("task_day_ledgers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("policy_revision_id", sa.String(36), sa.ForeignKey("managed_presence_policy_revisions.id"), nullable=False),
        sa.Column("canonical_peer_id", sa.String(120), nullable=False),
        sa.Column("task_day", sa.Date(), nullable=False),
        sa.Column("external_human_turn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("visible_managed_authored_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("planned_managed_authored_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trailing_managed_turn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("allowed_managed_authored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("remaining_capacity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_day_ledger_id", "policy_revision_id", name="uq_managed_presence_task_day_policy"),
    )
    op.create_index(
        "ix_managed_presence_peer_day",
        "managed_presence_plans",
        ["tenant_id", "canonical_peer_id", "task_day"],
    )
    op.create_table(
        "natural_opportunity_supply_plan_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_day_ledger_id", sa.String(36), sa.ForeignKey("task_day_ledgers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canonical_peer_id", sa.String(120), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("required_capacity", sa.Integer(), nullable=False),
        sa.Column("guaranteed_now_capacity", sa.Integer(), nullable=False),
        sa.Column("forecast_conditional_capacity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deficit", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("commitment_status", sa.String(40), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_day_ledger_id", "plan_revision", name="uq_natural_opportunity_plan_revision"),
    )
    op.create_index(
        "uq_natural_opportunity_plan_active",
        "natural_opportunity_supply_plan_revisions",
        ["task_day_ledger_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )
    op.create_table(
        "interaction_continuity_capacity_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_day_ledger_id", sa.String(36), sa.ForeignKey("task_day_ledgers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("policy_revision_id", sa.String(36), sa.ForeignKey("managed_presence_policy_revisions.id"), nullable=False),
        sa.Column("canonical_peer_id", sa.String(120), nullable=False),
        sa.Column("task_day", sa.Date(), nullable=False),
        sa.Column("observed_eligible_demand", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_service_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("protected_reserved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("borrowed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recalled_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("admitted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("served_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unknown_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected_by_capacity_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("remaining_capacity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_day_ledger_id", "policy_revision_id", name="uq_interaction_continuity_task_day_policy"),
    )
    op.create_index(
        "ix_interaction_continuity_peer_day",
        "interaction_continuity_capacity_plans",
        ["tenant_id", "canonical_peer_id", "task_day"],
    )
    op.add_column(
        "task_group_daily_message_slots",
        sa.Column("continuity_claim_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "task_group_daily_message_slots",
        sa.Column("quantity_credit_eligible", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_foreign_key(
        "fk_group_message_slot_continuity_claim",
        "task_group_daily_message_slots", "conversation_turn_claims",
        ["continuity_claim_id"], ["id"], ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_task_group_daily_message_slot_continuity_claim",
        "task_group_daily_message_slots", ["continuity_claim_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_task_group_daily_message_slot_continuity_claim",
        "task_group_daily_message_slots", type_="unique",
    )
    op.drop_constraint(
        "fk_group_message_slot_continuity_claim",
        "task_group_daily_message_slots", type_="foreignkey",
    )
    op.drop_column("task_group_daily_message_slots", "quantity_credit_eligible")
    op.drop_column("task_group_daily_message_slots", "continuity_claim_id")
    op.drop_index("ix_interaction_continuity_peer_day", table_name="interaction_continuity_capacity_plans")
    op.drop_table("interaction_continuity_capacity_plans")
    op.drop_index("uq_natural_opportunity_plan_active", table_name="natural_opportunity_supply_plan_revisions")
    op.drop_table("natural_opportunity_supply_plan_revisions")
    op.drop_index("ix_managed_presence_peer_day", table_name="managed_presence_plans")
    op.drop_table("managed_presence_plans")
    op.drop_index("uq_managed_presence_policy_active", table_name="managed_presence_policy_revisions")
    op.drop_table("managed_presence_policy_revisions")
