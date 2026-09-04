"""Persist measured samples and approved execution timing revisions.

Revision ID: 0215_execution_timing_profiles
Revises: 0214_source_journey_wiring
"""

from alembic import op
import sqlalchemy as sa


revision = "0215_execution_timing_profiles"
down_revision = "0214_source_journey_wiring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_samples()
    _create_profiles()
    op.create_index(
        "uq_execution_timing_profile_active", "execution_timing_profile_revisions",
        ["tenant_id", "adapter", "lane", "execution_path_hash"], unique=True,
        postgresql_where=sa.text("state = 'active'"), sqlite_where=sa.text("state = 'active'"),
    )


def _create_samples() -> None:
    op.create_table(
        "execution_timing_samples",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("adapter", sa.String(40), nullable=False),
        sa.Column("lane", sa.String(40), nullable=False),
        sa.Column("evidence_kind", sa.String(32), nullable=False),
        sa.Column("execution_path", sa.JSON(), nullable=False),
        sa.Column("execution_path_hash", sa.String(64), nullable=False),
        sa.Column("evidence_reference", sa.String(160), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("execution_attempt_id", sa.String(36), sa.ForeignKey("execution_attempts.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("boundary_timestamps", sa.JSON(), nullable=False),
        sa.Column("stage_durations_ms", sa.JSON(), nullable=False),
        sa.Column("remaining_path_ms", sa.JSON(), nullable=False),
        sa.Column("joint_path_ms", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "evidence_kind", "evidence_reference", name="uq_execution_timing_sample_evidence"),
    )


def _create_profiles() -> None:
    op.create_table(
        "execution_timing_profile_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("adapter", sa.String(40), nullable=False),
        sa.Column("lane", sa.String(40), nullable=False),
        sa.Column("profile_revision", sa.Integer(), nullable=False),
        sa.Column("execution_path", sa.JSON(), nullable=False),
        sa.Column("execution_path_hash", sa.String(64), nullable=False),
        sa.Column("policy_revision", sa.String(80), nullable=False),
        sa.Column("sample_ids", sa.JSON(), nullable=False),
        sa.Column("sample_manifest_hash", sa.String(64), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("minimum_sample_count", sa.Integer(), nullable=False),
        sa.Column("sample_window_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_window_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stage_p95_ms", sa.JSON(), nullable=False),
        sa.Column("remaining_path_p95_ms", sa.JSON(), nullable=False),
        sa.Column("joint_path_p95_ms", sa.JSON(), nullable=False),
        sa.Column("safety_margin_ms", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.String(32), nullable=False),
        sa.Column("approved_by", sa.String(160), nullable=False),
        sa.Column("approval_reference", sa.String(200), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("supersedes_profile_id", sa.String(36), sa.ForeignKey("execution_timing_profile_revisions.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "adapter", "lane", "execution_path_hash", "profile_revision", name="uq_execution_timing_profile_revision"),
        sa.UniqueConstraint("tenant_id", "adapter", "lane", "input_hash", name="uq_execution_timing_profile_input"),
    )


def downgrade() -> None:
    # SQLite enforces RESTRICT on the implicit row deletion during DROP TABLE.
    op.execute(sa.text("UPDATE execution_timing_profile_revisions SET supersedes_profile_id = NULL"))
    op.drop_table("execution_timing_profile_revisions")
    op.drop_table("execution_timing_samples")
