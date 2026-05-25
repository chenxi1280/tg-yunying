"""target learning profiles

Revision ID: 0051_target_learning_profiles
Revises: 0051_login_flow_failure_trace
Create Date: 2026-05-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0051_target_learning_profiles"
down_revision = "0051_login_flow_failure_trace"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def upgrade() -> None:
    if not _table_exists("target_learning_samples"):
        _create_samples_table()
    if not _table_exists("target_learning_profiles"):
        _create_profiles_table()
    if not _table_exists("target_learning_profile_versions"):
        _create_profile_versions_table()


def downgrade() -> None:
    for table_name in ("target_learning_profile_versions", "target_learning_profiles", "target_learning_samples"):
        if _table_exists(table_name):
            op.drop_table(table_name)


def _create_samples_table() -> None:
    op.create_table(
        "target_learning_samples",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=False),
        sa.Column("source_message_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("source_scene", sa.String(length=60), nullable=False, server_default="listener"),
        sa.Column("profile_scene", sa.String(length=60), nullable=False, server_default="group_chat"),
        sa.Column("sender_peer_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("sender_username", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("sender_name", sa.String(length=180), nullable=False, server_default=""),
        sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_managed_account", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("message_type", sa.String(length=40), nullable=False, server_default="text"),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("caption", sa.Text(), nullable=False, server_default=""),
        sa.Column("learning_status", sa.String(length=40), nullable=False, server_default="candidate"),
        sa.Column("reject_reason", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("downweight_reason", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("quality_score", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("observed_reply_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("applied_profile_version", sa.Integer(), nullable=True),
        sa.Column("status_updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "target_id", "profile_scene", "source_message_id", name="uq_target_learning_samples_message"),
    )
    op.create_index("ix_target_learning_samples_target_status", "target_learning_samples", ["target_id", "profile_scene", "learning_status"])


def _create_profiles_table() -> None:
    op.create_table(
        "target_learning_profiles",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=False),
        sa.Column("profile_scene", sa.String(length=60), nullable=False, server_default="group_chat"),
        sa.Column("learning_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("style_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("topic_weights", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("phrase_patterns", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("reply_patterns", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("comment_patterns", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("slang_terms", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("forbidden_learning", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("active_windows", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("profile_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("disabled_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_rebuilt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "target_id", "profile_scene", name="uq_target_learning_profiles_scene"),
    )
    op.create_index("ix_target_learning_profiles_target", "target_learning_profiles", ["target_id", "profile_scene"])


def _create_profile_versions_table() -> None:
    op.create_table(
        "target_learning_profile_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("profile_id", sa.String(length=36), sa.ForeignKey("target_learning_profiles.id"), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sample_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sample_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("quality_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.String(length=120), nullable=False, server_default="system"),
    )
    op.create_index("ix_target_learning_profile_versions_profile", "target_learning_profile_versions", ["profile_id", "profile_version"])
