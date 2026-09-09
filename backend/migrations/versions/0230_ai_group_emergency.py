"""Preserve group emergency content selection independently of generation outcomes."""
from alembic import op
import sqlalchemy as sa

revision = "0230_ai_group_emergency"
down_revision = "0229_admission_gap_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_group_emergency_selections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("action_id", sa.String(36), sa.ForeignKey("actions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("primary_quantity_slot_id", sa.String(36), sa.ForeignKey("task_group_daily_message_slots.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("generation_job_id", sa.String(36), sa.ForeignKey("generation_jobs.id", ondelete="RESTRICT")),
        sa.Column("policy_version", sa.String(48), nullable=False),
        sa.Column("previous_action_version", sa.Integer(), nullable=False),
        sa.Column("materialization_version", sa.Integer(), nullable=False),
        sa.Column("previous_payload_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source", sa.String(48), nullable=False),
        sa.Column("reason", sa.String(80), nullable=False),
        sa.Column("identity", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("action_id", name="uq_ai_group_emergency_action"),
        sa.UniqueConstraint("primary_quantity_slot_id", name="uq_ai_group_emergency_quantity"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM ai_group_emergency_selections")):
        raise RuntimeError("Emergency content ownership evidence must be preserved; use a forward fix")
    op.drop_table("ai_group_emergency_selections")
