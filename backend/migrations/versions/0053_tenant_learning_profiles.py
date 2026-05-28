"""tenant-level target profile models

Revision ID: 0053_tenant_learning_profiles
Revises: 0052_comment_author_identity
Create Date: 2026-05-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0053_tenant_learning_profiles"
down_revision = "0052_comment_author_identity"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _table_exists("tenant_learning_profiles"):
        _create_profiles()
    if not _table_exists("tenant_learning_sources"):
        _create_sources()
    if not _table_exists("tenant_learning_samples"):
        _create_samples()
    if not _table_exists("tenant_learning_quality_rules"):
        _create_quality_rules()
    if not _table_exists("tenant_learning_profile_versions"):
        _create_profile_versions()
    if not _table_exists("tenant_learning_runs"):
        _create_runs()


def downgrade() -> None:
    for table in (
        "tenant_learning_runs",
        "tenant_learning_profile_versions",
        "tenant_learning_quality_rules",
        "tenant_learning_samples",
        "tenant_learning_sources",
        "tenant_learning_profiles",
    ):
        if _table_exists(table):
            op.drop_table(table)


def _json_default(value: str) -> sa.TextClause:
    return sa.text(f"'{value}'::json")


def _create_profiles() -> None:
    op.create_table(
        "tenant_learning_profiles",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("profile_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="sample_insufficient"),
        sa.Column("learning_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("style_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("topic_weights", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("phrase_patterns", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("reply_patterns", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("comment_patterns", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("forbidden_learning", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("source_sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_rebuilt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_learning_profiles_tenant"),
    )


def _create_sources() -> None:
    op.create_table(
        "tenant_learning_sources",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=False),
        sa.Column("source_kind", sa.String(length=40), nullable=False, server_default="group"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auto_sync_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source_status", sa.String(length=40), nullable=False, server_default="active"),
        sa.Column("listener_account_ids", sa.JSON(), nullable=False, server_default=_json_default("[]")),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_history_pull_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("watermark", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("last_failure_detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("selected_by", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "target_id", name="uq_tenant_learning_sources_target"),
    )
    op.create_index("ix_tenant_learning_sources_status", "tenant_learning_sources", ["tenant_id", "source_status"])


def _create_samples() -> None:
    op.create_table(
        "tenant_learning_samples",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("source_id", sa.String(length=36), sa.ForeignKey("tenant_learning_sources.id"), nullable=False),
        sa.Column("source_message_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("source_scene", sa.String(length=60), nullable=False, server_default="listener"),
        sa.Column("sender_peer_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("sender_username", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("sender_name", sa.String(length=180), nullable=False, server_default=""),
        sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw_text_hash", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("learning_status", sa.String(length=40), nullable=False, server_default="candidate"),
        sa.Column("quality_score", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("quality_rule_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reject_reason", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("downweight_reason", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("decision_by", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "source_id", "source_message_id", name="uq_tenant_learning_samples_message"),
    )
    op.create_index("ix_tenant_learning_samples_status", "tenant_learning_samples", ["tenant_id", "learning_status"])


def _create_quality_rules() -> None:
    op.create_table(
        "tenant_learning_quality_rules",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("rule_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("identity_filters", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("text_filters", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("template_filters", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("scoring_thresholds", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("scene_weights", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("forbidden_patterns", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("updated_by", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "rule_version", name="uq_tenant_learning_quality_rules_version"),
    )


def _create_profile_versions() -> None:
    op.create_table(
        "tenant_learning_profile_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("profile_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("profile_snapshot", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("source_snapshot", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("quality_rule_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(length=120), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tenant_learning_profile_versions_profile", "tenant_learning_profile_versions", ["tenant_id", "profile_version"])


def _create_runs() -> None:
    op.create_table(
        "tenant_learning_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("run_type", sa.String(length=40), nullable=False, server_default="sync"),
        sa.Column("source_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="queued"),
        sa.Column("from_watermark", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("to_watermark", sa.JSON(), nullable=False, server_default=_json_default("{}")),
        sa.Column("pulled_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accepted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quality_rule_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("profile_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("trace_id", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tenant_learning_runs_status", "tenant_learning_runs", ["tenant_id", "run_type", "status"])
