"""Freeze approved execution timing on generation jobs without backfilling approval."""
from alembic import op
import sqlalchemy as sa

revision = "0216_generation_timing_binding"
down_revision = "0215_execution_timing_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generation_timing_bindings",
        sa.Column("generation_job_id", sa.String(36), sa.ForeignKey("generation_jobs.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False),
        sa.Column("adapter", sa.String(40), nullable=False),
        sa.Column("lane", sa.String(40), nullable=False),
        sa.Column("execution_path_hash", sa.String(64), nullable=False),
        sa.Column("timing_profile_id", sa.String(36), sa.ForeignKey("execution_timing_profile_revisions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("profile_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("resilience_policy_id", sa.String(36), sa.ForeignKey("execution_resilience_policy_revisions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("llm_timeout_ceiling_seconds", sa.Integer(), nullable=False),
        sa.Column("bound_send_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("generation_timing_bindings")
