"""ai group quality foundation

Revision ID: 0071_ai_group_quality_foundation
Revises: 0070_migrate_group_ai_topic_hint
Create Date: 2026-06-28 18:10:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0071_ai_group_quality_foundation"
down_revision = "0070_migrate_group_ai_topic_hint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_group_message_memory",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("action_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
        sa.Column("topic_direction", sa.Text(), nullable=False, server_default=""),
        sa.Column("teacher_target", sa.Text(), nullable=False, server_default=""),
        sa.Column("raw_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("normalized_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("text_fingerprint", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("semantic_cluster", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("template_shell_key", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("reservation_key", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="reserved"),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duplicate_window", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("duplicate_reference_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("quality_decision", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("profile_version", sa.Integer(), nullable=True),
        sa.Column("profile_match_score", sa.Integer(), nullable=True),
        sa.Column("profile_match_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("result", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_ai_group_message_memory_dedupe",
        "ai_group_message_memory",
        ["tenant_id", "group_id", "text_fingerprint", "status", "planned_at"],
    )
    op.create_index("ix_ai_group_message_memory_expiry", "ai_group_message_memory", ["status", "expires_at"])
    op.create_index("ix_ai_group_message_memory_task", "ai_group_message_memory", ["tenant_id", "task_id", "planned_at"])
    op.create_index(
        "uq_ai_group_message_memory_reservation_key",
        "ai_group_message_memory",
        ["reservation_key"],
        unique=True,
        postgresql_where=sa.text("reservation_key <> ''"),
    )

    op.create_table(
        "ai_account_voice_profiles",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("age_band", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("persona_experiences", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("consumption_experiences", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("sentence_length", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("interaction_habits", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("tone_strength", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("lexical_preferences", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("emoji_policy", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("forbidden_expressions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("short_prompt_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="ai_batch"),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="active"),
        sa.Column("similarity_score", sa.Integer(), nullable=True),
        sa.Column("quality_status", sa.String(length=40), nullable=False, server_default="active"),
        sa.Column("last_rebuilt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(length=120), nullable=False, server_default="system"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "account_id", "version", name="uq_ai_account_voice_profiles_version"),
    )
    op.create_index(
        "ix_ai_account_voice_profiles_account_status",
        "ai_account_voice_profiles",
        ["tenant_id", "account_id", "status"],
    )

    op.create_table(
        "ai_account_group_stance_memory",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("topic_direction", sa.Text(), nullable=False, server_default=""),
        sa.Column("teacher_target", sa.Text(), nullable=False, server_default=""),
        sa.Column("stance", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("last_act_type", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("last_semantic_cluster", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("last_message_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("last_spoken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "group_id", "account_id", name="uq_ai_account_group_stance"),
    )
    op.create_index(
        "ix_ai_account_group_stance_group",
        "ai_account_group_stance_memory",
        ["tenant_id", "group_id", "account_id"],
    )

    op.create_table(
        "tg_account_online_state",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("desired_online", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("desired_sources", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("online_status", sa.String(length=40), nullable=False, server_default="offline"),
        sa.Column("session_kind", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("session_id", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("proxy_id", sa.Integer(), sa.ForeignKey("account_proxies.id"), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_keepalive_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stale_after_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_type", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("recovery_status", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("next_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_task_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "account_id", name="uq_tg_account_online_state_account"),
    )
    op.create_index("ix_tg_account_online_state_status", "tg_account_online_state", ["tenant_id", "desired_online", "online_status"])
    op.create_index("ix_tg_account_online_state_next_probe", "tg_account_online_state", ["tenant_id", "next_probe_at"])


def downgrade() -> None:
    op.drop_index("ix_tg_account_online_state_next_probe", table_name="tg_account_online_state")
    op.drop_index("ix_tg_account_online_state_status", table_name="tg_account_online_state")
    op.drop_table("tg_account_online_state")
    op.drop_index("ix_ai_account_group_stance_group", table_name="ai_account_group_stance_memory")
    op.drop_table("ai_account_group_stance_memory")
    op.drop_index("ix_ai_account_voice_profiles_account_status", table_name="ai_account_voice_profiles")
    op.drop_table("ai_account_voice_profiles")
    op.drop_index("uq_ai_group_message_memory_reservation_key", table_name="ai_group_message_memory")
    op.drop_index("ix_ai_group_message_memory_task", table_name="ai_group_message_memory")
    op.drop_index("ix_ai_group_message_memory_expiry", table_name="ai_group_message_memory")
    op.drop_index("ix_ai_group_message_memory_dedupe", table_name="ai_group_message_memory")
    op.drop_table("ai_group_message_memory")
