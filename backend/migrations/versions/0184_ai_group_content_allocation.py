"""Add immutable AI group content allocation plans and intents.

Revision ID: 0184_ai_group_content_alloc
Revises: 0183_clone_event_config_snapshot

The initial migration builds current metadata on a fresh database, so this
migration must not recreate tables that already exist in that path.
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0184_ai_group_content_alloc"
down_revision = "0183_clone_event_config_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if _has_table("ai_group_content_allocation_plans"):
        return
    op.create_table(
        "ai_group_content_allocation_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("task_day_ledger_id", sa.String(length=36), nullable=False),
        sa.Column("target_operation_target_id", sa.Integer(), nullable=False),
        sa.Column("task_day", sa.Date(), nullable=False),
        sa.Column("route_family", sa.String(length=24), nullable=False),
        sa.Column("surface_scope_key", sa.String(length=255), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("config_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("topic_rate_bps", sa.Integer(), nullable=False),
        sa.Column("normal_text_cursor", sa.Integer(), nullable=False),
        sa.Column("question_count", sa.Integer(), nullable=False),
        sa.Column("daily_vocabulary_theme_id", sa.Integer(), nullable=False),
        sa.Column("daily_vocabulary_theme_version", sa.String(length=32), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_day_ledger_id"], ["task_day_ledgers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_operation_target_id"], ["operation_targets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_day_ledger_id", "target_operation_target_id", "route_family",
            name="uq_ai_group_content_plan_surface_day",
        ),
    )
    op.create_index(
        "ix_ai_group_content_plan_scope",
        "ai_group_content_allocation_plans",
        ["surface_scope_key", "task_day"],
    )
    op.create_table(
        "ai_group_content_intents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("allocation_plan_id", sa.String(length=36), nullable=False),
        sa.Column("primary_quantity_slot_id", sa.String(length=36), nullable=False),
        sa.Column("normal_text_ordinal", sa.Integer(), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("config_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False),
        sa.Column("target_reference_revision", sa.Integer(), nullable=False),
        sa.Column("relation_kind", sa.String(length=16), nullable=False),
        sa.Column("act_type", sa.String(length=32), nullable=False),
        sa.Column("stance", sa.String(length=24), nullable=False),
        sa.Column("topic_budget_eligible", sa.Boolean(), nullable=False),
        sa.Column("topic_mode", sa.String(length=32), nullable=False),
        sa.Column("topic_direction_snapshot", sa.JSON(), nullable=False),
        sa.Column("teacher_target_snapshot", sa.JSON(), nullable=False),
        sa.Column("topic_capacity_reservation_id", sa.String(length=36), nullable=False),
        sa.Column("daily_vocabulary_theme_id", sa.Integer(), nullable=False),
        sa.Column("daily_vocabulary_theme_effective_state", sa.String(length=48), nullable=False),
        sa.Column("vocabulary_catalog_version", sa.String(length=32), nullable=False),
        sa.Column("vocabulary_sample_ids", sa.JSON(), nullable=False),
        sa.Column("vocabulary_surface_terms", sa.JSON(), nullable=False),
        sa.Column("vocabulary_normalized_term_ids", sa.JSON(), nullable=False),
        sa.Column("vocabulary_candidate_count", sa.Integer(), nullable=False),
        sa.Column("vocabulary_reservation_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["allocation_plan_id"], ["ai_group_content_allocation_plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["primary_quantity_slot_id"], ["task_group_daily_message_slots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("allocation_plan_id", "normal_text_ordinal", name="uq_ai_group_content_intent_ordinal"),
        sa.UniqueConstraint("primary_quantity_slot_id", name="uq_ai_group_content_intent_quantity_slot"),
    )
    op.create_index(
        "ix_ai_group_content_intent_plan_created",
        "ai_group_content_intents",
        ["allocation_plan_id", "created_at"],
    )


def _has_table(table_name: str) -> bool:
    if context.is_offline_mode():
        return False
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def downgrade() -> None:
    op.drop_index("ix_ai_group_content_intent_plan_created", table_name="ai_group_content_intents")
    op.drop_table("ai_group_content_intents")
    op.drop_index("ix_ai_group_content_plan_scope", table_name="ai_group_content_allocation_plans")
    op.drop_table("ai_group_content_allocation_plans")
