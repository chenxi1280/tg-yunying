"""Add channel comment source and distinct-account plan contracts.

Revision ID: 0188_comment_plan_contract
Revises: 0187_comment_material_integrity
"""

from alembic import op
import sqlalchemy as sa


revision = "0188_comment_plan_contract"
down_revision = "0187_comment_material_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_source_revisions()
    _create_plan_contracts()
    _create_eligible_rows()
    _create_bindings()
    _create_grounding_assignments()
    _extend_existing_tables()
    _create_capacity_periods()
    _create_capacity_reservations()


def _create_source_revisions() -> None:
    op.create_table(
        "channel_message_source_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel_message_id", sa.Integer(), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("source_remote_message_id", sa.Integer(), nullable=False),
        sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_text_snapshot", sa.Text(), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column("observation_identity_hash", sa.String(length=64), nullable=False),
        sa.Column("source_operation", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_message_id"], ["channel_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_message_id", "source_revision", name="uq_channel_message_source_revision"),
        sa.UniqueConstraint("observation_identity_hash", name="uq_channel_message_source_observation"),
    )


def _create_plan_contracts() -> None:
    op.create_table(
        "channel_comment_plan_contracts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("channel_message_id", sa.Integer(), nullable=False),
        sa.Column("comment_plan_revision", sa.Integer(), nullable=False),
        sa.Column("source_revision_id", sa.String(length=36), nullable=False),
        sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("eligible_account_count", sa.Integer(), nullable=False),
        sa.Column("eligible_account_ids_hash", sa.String(length=64), nullable=False),
        sa.Column("participation_seed", sa.String(length=128), nullable=False),
        sa.Column("effective_participation_bps", sa.Integer(), nullable=False),
        sa.Column("required_distinct_account_count", sa.Integer(), nullable=False),
        sa.Column("grounding_required_count", sa.Integer(), nullable=False),
        sa.Column("planned_fallback_count", sa.Integer(), nullable=False),
        sa.Column("daily_comment_cap", sa.Integer(), nullable=False),
        sa.Column("quantity_contract_version", sa.String(length=48), nullable=False),
        sa.Column("contract_state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_message_id"], ["channel_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_revision_id"], ["channel_message_source_revisions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "channel_message_id", "comment_plan_revision", name="uq_channel_comment_plan_revision"),
    )


def _create_eligible_rows() -> None:
    op.create_table(
        "channel_comment_eligible_account_snapshot_rows",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("plan_contract_id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("eligibility_state", sa.String(length=32), nullable=False),
        sa.Column("stable_rank", sa.Integer(), nullable=False),
        sa.Column("eligibility_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["tg_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["plan_contract_id"], ["channel_comment_plan_contracts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_contract_id", "account_id", name="uq_channel_comment_plan_eligible_account"),
    )


def _create_bindings() -> None:
    op.create_table(
        "channel_comment_ordinal_account_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("plan_contract_id", sa.String(length=36), nullable=False),
        sa.Column("target_ordinal", sa.Integer(), nullable=False),
        sa.Column("binding_attempt", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("binding_state", sa.String(length=32), nullable=False),
        sa.Column("replacement_reason", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["tg_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["plan_contract_id"], ["channel_comment_plan_contracts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_contract_id", "target_ordinal", "binding_attempt", name="uq_channel_comment_ordinal_binding_attempt"),
        sa.UniqueConstraint("plan_contract_id", "account_id", "binding_attempt", name="uq_channel_comment_plan_account_binding_attempt"),
    )


def _create_grounding_assignments() -> None:
    op.create_table(
        "channel_comment_grounding_assignments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("plan_contract_id", sa.String(length=36), nullable=False),
        sa.Column("source_revision_id", sa.String(length=36), nullable=False),
        sa.Column("target_ordinal", sa.Integer(), nullable=False),
        sa.Column("assignment_version", sa.Integer(), nullable=False),
        sa.Column("evidence_text", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("primary_aspect_code", sa.String(length=64), nullable=False),
        sa.Column("primary_aspect_text", sa.Text(), nullable=False),
        sa.Column("teacher_name", sa.String(length=160), nullable=False),
        sa.Column("speech_act", sa.String(length=48), nullable=False),
        sa.Column("assignment_state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["plan_contract_id"], ["channel_comment_plan_contracts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_revision_id"], ["channel_message_source_revisions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_contract_id", "target_ordinal", "assignment_version", name="uq_channel_comment_grounding_assignment"),
    )


def _extend_existing_tables() -> None:
    op.add_column("channel_messages", sa.Column("current_source_revision_id", sa.String(length=36), nullable=True))
    op.create_foreign_key(
        "fk_channel_message_current_source_revision",
        "channel_messages", "channel_message_source_revisions",
        ["current_source_revision_id"], ["id"], ondelete="SET NULL",
    )
    op.add_column("comment_fulfillment_obligations", sa.Column("plan_contract_id", sa.String(length=36), nullable=True))
    op.add_column("comment_fulfillment_obligations", sa.Column("account_id", sa.Integer(), nullable=True))
    op.add_column("comment_fulfillment_obligations", sa.Column("grounding_assignment_id", sa.String(length=36), nullable=True))
    op.add_column("comment_fulfillment_obligations", sa.Column("fallback_intent_kind", sa.String(length=24), nullable=False, server_default="emergency"))
    op.create_foreign_key(
        "fk_comment_obligation_plan_contract",
        "comment_fulfillment_obligations", "channel_comment_plan_contracts",
        ["plan_contract_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_comment_obligation_account",
        "comment_fulfillment_obligations", "tg_accounts",
        ["account_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_comment_obligation_grounding_assignment",
        "comment_fulfillment_obligations", "channel_comment_grounding_assignments",
        ["grounding_assignment_id"], ["id"], ondelete="RESTRICT",
    )
    op.alter_column("comment_fulfillment_obligations", "fallback_intent_kind", server_default=None)


def _create_capacity_periods() -> None:
    op.create_table(
        "task_comment_capacity_periods",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("calendar_revision", sa.Integer(), nullable=False),
        sa.Column("timezone_snapshot", sa.String(length=80), nullable=False),
        sa.Column("period_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capacity_limit", sa.Integer(), nullable=False),
        sa.Column("period_state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "period_start_at", name="uq_task_comment_capacity_period_start"),
    )


def _create_capacity_reservations() -> None:
    op.create_table(
        "task_comment_capacity_reservations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("capacity_period_id", sa.String(length=36), nullable=False),
        sa.Column("plan_contract_id", sa.String(length=36), nullable=False),
        sa.Column("obligation_id", sa.String(length=36), nullable=False),
        sa.Column("action_id", sa.String(length=36), nullable=True),
        sa.Column("capacity_units", sa.Integer(), nullable=False),
        sa.Column("reservation_state", sa.String(length=32), nullable=False),
        sa.Column("scheduled_for_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["action_id"], ["actions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["capacity_period_id"], ["task_comment_capacity_periods.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["obligation_id"], ["comment_fulfillment_obligations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_contract_id"], ["channel_comment_plan_contracts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("obligation_id", name="uq_task_comment_capacity_obligation"),
    )


def downgrade() -> None:
    op.drop_table("task_comment_capacity_reservations")
    op.drop_table("task_comment_capacity_periods")
    op.drop_constraint("fk_comment_obligation_grounding_assignment", "comment_fulfillment_obligations", type_="foreignkey")
    op.drop_constraint("fk_comment_obligation_account", "comment_fulfillment_obligations", type_="foreignkey")
    op.drop_constraint("fk_comment_obligation_plan_contract", "comment_fulfillment_obligations", type_="foreignkey")
    op.drop_column("comment_fulfillment_obligations", "fallback_intent_kind")
    op.drop_column("comment_fulfillment_obligations", "account_id")
    op.drop_column("comment_fulfillment_obligations", "grounding_assignment_id")
    op.drop_column("comment_fulfillment_obligations", "plan_contract_id")
    op.drop_constraint("fk_channel_message_current_source_revision", "channel_messages", type_="foreignkey")
    op.drop_column("channel_messages", "current_source_revision_id")
    op.drop_table("channel_comment_ordinal_account_bindings")
    op.drop_table("channel_comment_grounding_assignments")
    op.drop_table("channel_comment_eligible_account_snapshot_rows")
    op.drop_table("channel_comment_plan_contracts")
    op.drop_table("channel_message_source_revisions")
