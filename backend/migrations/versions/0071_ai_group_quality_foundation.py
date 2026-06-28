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
    _create_ai_group_message_memory()
    _create_ai_account_voice_profiles()
    _create_ai_account_group_stance_memory()
    _create_tg_account_online_state()


def downgrade() -> None:
    _drop_index_if_exists("ix_tg_account_online_state_next_probe", "tg_account_online_state")
    _drop_index_if_exists("ix_tg_account_online_state_status", "tg_account_online_state")
    _drop_table_if_exists("tg_account_online_state")
    _drop_index_if_exists("ix_ai_account_group_stance_group", "ai_account_group_stance_memory")
    _drop_table_if_exists("ai_account_group_stance_memory")
    _drop_index_if_exists("ix_ai_account_voice_profiles_account_status", "ai_account_voice_profiles")
    _drop_table_if_exists("ai_account_voice_profiles")
    _drop_index_if_exists("uq_ai_group_message_memory_reservation_key", "ai_group_message_memory")
    _drop_index_if_exists("ix_ai_group_message_memory_task", "ai_group_message_memory")
    _drop_index_if_exists("ix_ai_group_message_memory_expiry", "ai_group_message_memory")
    _drop_index_if_exists("ix_ai_group_message_memory_dedupe", "ai_group_message_memory")
    _drop_table_if_exists("ai_group_message_memory")


def _create_ai_group_message_memory() -> None:
    if not _has_table("ai_group_message_memory"):
        _create_ai_group_message_memory_table()
    _create_index_if_missing(
        "ix_ai_group_message_memory_dedupe",
        "ai_group_message_memory",
        ["tenant_id", "group_id", "text_fingerprint", "status", "planned_at"],
    )
    _create_index_if_missing("ix_ai_group_message_memory_expiry", "ai_group_message_memory", ["status", "expires_at"])
    _create_index_if_missing("ix_ai_group_message_memory_task", "ai_group_message_memory", ["tenant_id", "task_id", "planned_at"])
    _create_index_if_missing(
        "uq_ai_group_message_memory_reservation_key",
        "ai_group_message_memory",
        ["reservation_key"],
        unique=True,
        postgresql_where=sa.text("reservation_key <> ''"),
    )


def _create_ai_group_message_memory_table() -> None:
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


def _create_ai_account_voice_profiles() -> None:
    if not _has_table("ai_account_voice_profiles"):
        _create_ai_account_voice_profiles_table()
    _create_index_if_missing(
        "ix_ai_account_voice_profiles_account_status",
        "ai_account_voice_profiles",
        ["tenant_id", "account_id", "status"],
    )


def _create_ai_account_voice_profiles_table() -> None:
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


def _create_ai_account_group_stance_memory() -> None:
    if not _has_table("ai_account_group_stance_memory"):
        _create_ai_account_group_stance_memory_table()
    _create_index_if_missing(
        "ix_ai_account_group_stance_group",
        "ai_account_group_stance_memory",
        ["tenant_id", "group_id", "account_id"],
    )


def _create_ai_account_group_stance_memory_table() -> None:
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


def _create_tg_account_online_state() -> None:
    if not _has_table("tg_account_online_state"):
        _create_tg_account_online_state_table()
    _create_index_if_missing(
        "ix_tg_account_online_state_status",
        "tg_account_online_state",
        ["tenant_id", "desired_online", "online_status"],
    )
    _create_index_if_missing(
        "ix_tg_account_online_state_next_probe",
        "tg_account_online_state",
        ["tenant_id", "next_probe_at"],
    )


def _create_tg_account_online_state_table() -> None:
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


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return index_name in {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _create_index_if_missing(index_name: str, table_name: str, columns: list[str], **kwargs) -> None:
    if not _has_index(table_name, index_name):
        op.create_index(index_name, table_name, columns, **kwargs)


def _drop_index_if_exists(index_name: str, table_name: str) -> None:
    if _has_index(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)


def _drop_table_if_exists(table_name: str) -> None:
    if _has_table(table_name):
        op.drop_table(table_name)
