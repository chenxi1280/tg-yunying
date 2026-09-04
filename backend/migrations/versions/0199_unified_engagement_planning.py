"""Add unified engagement participation and admission plans.

Revision ID: 0199_unified_engagement_planning
Revises: 0198_unified_engagement_runtime
"""

from alembic import op
import sqlalchemy as sa


revision = "0199_unified_engagement_planning"
down_revision = "0198_unified_engagement_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_participation_plans()
    _create_admission_snapshots()
    _extend_comment_plan_contract()
    _extend_group_daily_target()
    _create_behavior_sessions()
    _create_reaction_capacity_epochs()
    _create_view_allocation_plans()


def _create_participation_plans() -> None:
    table = "task_participation_unit_plans"
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("task_day_ledger_id", sa.String(36), sa.ForeignKey("task_day_ledgers.id", ondelete="CASCADE"), nullable=True),
        sa.Column("membership_snapshot_set_id", sa.String(36), sa.ForeignKey("account_group_membership_snapshot_sets.id"), nullable=False),
        sa.Column("participation_kind", sa.String(48), nullable=False),
        sa.Column("participation_unit", sa.String(200), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("policy_revision", sa.String(64), nullable=False),
        sa.Column("policy_eligible_account_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("selected_account_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("selected_origin_groups", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("sampled_ratio_bps", sa.Integer(), nullable=True),
        sa.Column("rounded_selected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("participation_min_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("participation_max_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("realized_participation_bps", sa.Integer(), nullable=True),
        sa.Column("integer_quantization_adjustment", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("required_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("selection_seed", sa.String(64), nullable=False),
        sa.Column("selection_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "task_lifecycle_epoch", "participation_kind", "participation_unit", "plan_revision", name="uq_task_participation_unit_revision"),
    )
    _active_index(
        "uq_task_participation_unit_active",
        table,
        ["task_id", "task_lifecycle_epoch", "participation_kind", "participation_unit"],
    )


def _create_admission_snapshots() -> None:
    table = "planning_admission_snapshots"
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("participation_plan_id", sa.String(36), sa.ForeignKey("task_participation_unit_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("participation_unit", sa.String(200), nullable=False),
        sa.Column("planning_horizon", sa.String(100), nullable=False),
        sa.Column("dependency_revision_set_hash", sa.String(64), nullable=False),
        sa.Column("account_paths", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("admissible_account_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("deficit_account_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "task_lifecycle_epoch", "participation_unit", "planning_horizon", "dependency_revision_set_hash", name="uq_planning_admission_dependency_set"),
    )
    op.create_index("ix_planning_admission_plan", table, ["participation_plan_id", "decision"])


def _extend_comment_plan_contract() -> None:
    table = "channel_comment_plan_contracts"
    op.add_column(table, sa.Column("task_day_ledger_id", sa.String(36), nullable=True))
    op.add_column(table, sa.Column("daily_participation_plan_id", sa.String(36), nullable=True))
    op.add_column(table, sa.Column("planning_admission_snapshot_id", sa.String(36), nullable=True))
    op.add_column(table, sa.Column("source_participation_plan_id", sa.String(36), nullable=True))
    op.add_column(table, sa.Column("rounded_required_distinct_account_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(table, sa.Column("realized_participation_bps", sa.Integer(), nullable=True))
    op.add_column(table, sa.Column("integer_quantization_adjustment", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_foreign_key("fk_comment_plan_task_day_ledger", table, "task_day_ledgers", ["task_day_ledger_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_comment_plan_daily_participation", table, "task_participation_unit_plans", ["daily_participation_plan_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_comment_plan_planning_admission", table, "planning_admission_snapshots", ["planning_admission_snapshot_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_comment_plan_source_participation", table, "task_participation_unit_plans", ["source_participation_plan_id"], ["id"], ondelete="RESTRICT")


def _extend_group_daily_target() -> None:
    table = "task_group_daily_targets"
    op.add_column(table, sa.Column("participation_plan_id", sa.String(36), nullable=True))
    op.add_column(table, sa.Column("planning_admission_snapshot_id", sa.String(36), nullable=True))
    op.add_column(table, sa.Column("quantity_policy_revision", sa.String(64), nullable=False, server_default="legacy_fixed_v0"))
    op.add_column(table, sa.Column("quantity_seed", sa.String(64), nullable=False, server_default=""))
    op.add_column(table, sa.Column("sampled_jitter_bps", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(table, sa.Column("raw_quantity_target", sa.Integer(), nullable=False, server_default="0"))
    op.create_foreign_key("fk_group_daily_target_participation", table, "task_participation_unit_plans", ["participation_plan_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_group_daily_target_planning_admission", table, "planning_admission_snapshots", ["planning_admission_snapshot_id"], ["id"], ondelete="RESTRICT")


def _create_behavior_sessions() -> None:
    table = "account_behavior_session_plans"
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_day", sa.Date(), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("policy_revision_id", sa.String(36), sa.ForeignKey("account_behavior_budget_policy_revisions.id"), nullable=False),
        sa.Column("chronotype", sa.String(32), nullable=False),
        sa.Column("weekday_class", sa.String(24), nullable=False),
        sa.Column("windows", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("visible_action_capacity", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("rest_debt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wake_policy", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("seed", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "account_id", "task_day", "plan_revision", name="uq_behavior_session_account_day_revision"),
    )
    _active_index(
        "uq_behavior_session_account_day_active",
        table,
        ["tenant_id", "account_id", "task_day"],
    )


def _active_index(name: str, table: str, columns: list[str]) -> None:
    op.create_index(
        name,
        table,
        columns,
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )


def _create_reaction_capacity_epochs() -> None:
    table = "reaction_capacity_allocation_epochs"
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("task_day_ledger_id", sa.String(36), sa.ForeignKey("task_day_ledgers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("allocation_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("policy_revision", sa.String(64), nullable=False),
        sa.Column("daily_reaction_cap", sa.Integer(), nullable=False),
        sa.Column("source_demands", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source_allocations", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("planning_admission_snapshot_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("allocated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unallocated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unallocated_reasons", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("allocation_hash", sa.String(64), nullable=False),
        sa.Column("supersedes_epoch_id", sa.String(36), sa.ForeignKey("reaction_capacity_allocation_epochs.id"), nullable=True),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "task_lifecycle_epoch", "task_day_ledger_id", "allocation_revision", name="uq_reaction_capacity_epoch_revision"),
    )
    _active_index(
        "uq_reaction_capacity_epoch_active",
        table,
        ["task_id", "task_lifecycle_epoch", "task_day_ledger_id"],
    )


def _create_view_allocation_plans() -> None:
    table = "view_account_source_allocation_plans"
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("task_day_ledger_id", sa.String(36), sa.ForeignKey("task_day_ledgers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("participation_plan_id", sa.String(36), sa.ForeignKey("task_participation_unit_plans.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("planning_admission_snapshot_id", sa.String(36), sa.ForeignKey("planning_admission_snapshots.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("allocation_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("allocation_mode", sa.String(32), nullable=False),
        sa.Column("algorithm_revision", sa.String(64), nullable=False),
        sa.Column("source_set", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source_set_hash", sa.String(64), nullable=False),
        sa.Column("account_degrees", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source_exposures", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("edge_set", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("edge_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unallocated_sources", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column("allocation_seed", sa.String(64), nullable=False),
        sa.Column("allocation_hash", sa.String(64), nullable=False),
        sa.Column("supersedes_plan_id", sa.String(36), sa.ForeignKey("view_account_source_allocation_plans.id"), nullable=True),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "task_lifecycle_epoch", "task_day_ledger_id", "allocation_revision", name="uq_view_account_source_allocation_revision"),
    )
    _active_index(
        "uq_view_account_source_allocation_active",
        table,
        ["task_id", "task_lifecycle_epoch", "task_day_ledger_id"],
    )


def downgrade() -> None:
    op.drop_table("view_account_source_allocation_plans")
    op.drop_table("reaction_capacity_allocation_epochs")
    op.drop_table("account_behavior_session_plans")
    op.drop_constraint("fk_group_daily_target_participation", "task_group_daily_targets", type_="foreignkey")
    op.drop_constraint("fk_group_daily_target_planning_admission", "task_group_daily_targets", type_="foreignkey")
    for column in (
        "raw_quantity_target",
        "sampled_jitter_bps",
        "quantity_seed",
        "quantity_policy_revision",
        "participation_plan_id",
        "planning_admission_snapshot_id",
    ):
        op.drop_column("task_group_daily_targets", column)
    op.drop_constraint("fk_comment_plan_source_participation", "channel_comment_plan_contracts", type_="foreignkey")
    op.drop_constraint("fk_comment_plan_daily_participation", "channel_comment_plan_contracts", type_="foreignkey")
    op.drop_constraint("fk_comment_plan_planning_admission", "channel_comment_plan_contracts", type_="foreignkey")
    op.drop_constraint("fk_comment_plan_task_day_ledger", "channel_comment_plan_contracts", type_="foreignkey")
    for column in (
        "integer_quantization_adjustment",
        "realized_participation_bps",
        "rounded_required_distinct_account_count",
        "source_participation_plan_id",
        "daily_participation_plan_id",
        "planning_admission_snapshot_id",
        "task_day_ledger_id",
    ):
        op.drop_column("channel_comment_plan_contracts", column)
    op.drop_table("planning_admission_snapshots")
    op.drop_table("task_participation_unit_plans")
