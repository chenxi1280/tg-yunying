"""Add complete channel comment grounding facts and evaluations.

Revision ID: 0195_comment_grounding_snapshot
Revises: 0194_channel_comment_discussion
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0195_comment_grounding_snapshot"
down_revision = "0194_channel_comment_discussion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _add_source_fact_columns()
    _create_grounding_snapshots()
    _add_assignment_fact_columns()
    _create_grounding_evaluations()


def _add_source_fact_columns() -> None:
    table = "channel_message_source_revisions"
    columns = (
        sa.Column("channel_target_id", sa.Integer(), sa.ForeignKey("operation_targets.id", ondelete="CASCADE")),
        sa.Column("source_published_at_fact_id", sa.String(160), nullable=False, server_default=""),
        sa.Column("telegram_edit_date", sa.DateTime(timezone=True)),
        sa.Column("source_type", sa.String(24), nullable=False, server_default="message_text"),
        sa.Column("source_length", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("captured_length", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncation_state", sa.String(32), nullable=False, server_default="legacy_unproven"),
    )
    _add_columns(table, columns)


def _create_grounding_snapshots() -> None:
    table = "channel_comment_grounding_snapshots"
    if _has_table(table):
        return
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("comment_plan_contract_id", sa.String(36), sa.ForeignKey("channel_comment_plan_contracts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_target_id", sa.Integer(), sa.ForeignKey("operation_targets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_message_id", sa.Integer(), sa.ForeignKey("channel_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_remote_message_id", sa.Integer(), nullable=False),
        sa.Column("source_revision_id", sa.String(36), sa.ForeignKey("channel_message_source_revisions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("comment_grounding_revision", sa.Integer(), nullable=False),
        sa.Column("supersedes_snapshot_id", sa.String(36), sa.ForeignKey(f"{table}.id", ondelete="RESTRICT")),
        sa.Column("grounding_contract_version", sa.String(64), nullable=False),
        sa.Column("grounding_policy_version", sa.String(64), nullable=False),
        sa.Column("extractor_version", sa.String(64), nullable=False),
        sa.Column("content_route", sa.String(48), nullable=False),
        sa.Column("content_route_revision", sa.Integer(), nullable=False),
        sa.Column("source_content_hash", sa.String(64), nullable=False),
        sa.Column("source_state", sa.String(32), nullable=False),
        sa.Column("teacher_state", sa.String(32), nullable=False),
        sa.Column("teacher_candidates_json", sa.JSON(), nullable=False),
        sa.Column("aspect_evidence_json", sa.JSON(), nullable=False),
        sa.Column("evidence_blocks_json", sa.JSON(), nullable=False),
        sa.Column("semantic_capacity_policy_version", sa.String(64), nullable=False),
        sa.Column("semantic_variant_units_json", sa.JSON(), nullable=False),
        sa.Column("groundable_capacity_count", sa.Integer(), nullable=False),
        sa.Column("extraction_audit_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "task_id", "channel_message_id", "comment_grounding_revision", name="uq_channel_comment_grounding_scope_revision"),
        sa.UniqueConstraint("comment_plan_contract_id", "comment_grounding_revision", name="uq_channel_comment_grounding_plan_revision"),
        sa.UniqueConstraint("comment_plan_contract_id", "source_revision_id", "grounding_policy_version", name="uq_channel_comment_grounding_source_policy"),
    )


def _add_assignment_fact_columns() -> None:
    table = "channel_comment_grounding_assignments"
    columns = (
        sa.Column("grounding_snapshot_id", sa.String(36), sa.ForeignKey("channel_comment_grounding_snapshots.id", ondelete="RESTRICT")),
        sa.Column("comment_grounding_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("teacher_candidate_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("primary_evidence_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("secondary_evidence_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("relation_kind", sa.String(24), nullable=False, server_default="direct"),
    )
    _add_columns(table, columns)


def _create_grounding_evaluations() -> None:
    table = "channel_comment_grounding_evaluations"
    if _has_table(table):
        return
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_id", sa.String(36), sa.ForeignKey("actions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("generation_job_id", sa.String(36), sa.ForeignKey("generation_jobs.id", ondelete="SET NULL")),
        sa.Column("generation_attempt_id", sa.String(80), nullable=False),
        sa.Column("candidate_content_hash", sa.String(64), nullable=False),
        sa.Column("deterministic_evaluator_version", sa.String(64), nullable=False),
        sa.Column("semantic_reviewer_request_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("semantic_reviewer_model", sa.String(120), nullable=False, server_default=""),
        sa.Column("semantic_reviewer_schema_version", sa.String(64), nullable=False, server_default=""),
        sa.Column("semantic_reviewer_prompt_version", sa.String(64), nullable=False, server_default=""),
        sa.Column("semantic_reviewer_input_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("claim_results_json", sa.JSON(), nullable=False),
        sa.Column("primary_aspect_result", sa.String(24), nullable=False),
        sa.Column("reply_relation_result", sa.String(24), nullable=False),
        sa.Column("final_result", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("action_id", "generation_attempt_id", "candidate_content_hash", name="uq_channel_comment_grounding_evaluation_candidate"),
    )


def _add_columns(table: str, columns: tuple[sa.Column, ...]) -> None:
    for column in columns:
        if not _has_column(table, column.name):
            op.add_column(table, column)


def _has_table(table: str) -> bool:
    if context.is_offline_mode():
        return False
    return sa.inspect(op.get_bind()).has_table(table)


def _has_column(table: str, column: str) -> bool:
    if context.is_offline_mode():
        return False
    return any(row["name"] == column for row in sa.inspect(op.get_bind()).get_columns(table))


def downgrade() -> None:
    if _has_table("channel_comment_grounding_evaluations"):
        op.drop_table("channel_comment_grounding_evaluations")
    for column in (
        "relation_kind", "secondary_evidence_id", "primary_evidence_id",
        "teacher_candidate_id", "comment_grounding_revision", "grounding_snapshot_id",
    ):
        if _has_column("channel_comment_grounding_assignments", column):
            op.drop_column("channel_comment_grounding_assignments", column)
    if _has_table("channel_comment_grounding_snapshots"):
        op.drop_table("channel_comment_grounding_snapshots")
    for column in (
        "truncation_state", "captured_length", "source_length", "source_type",
        "telegram_edit_date", "source_published_at_fact_id", "channel_target_id",
    ):
        if _has_column("channel_message_source_revisions", column):
            op.drop_column("channel_message_source_revisions", column)
