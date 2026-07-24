"""Humanized interaction speaker state and group-bot admission.

Revision ID: 0120_humanized_interaction_admission
Revises: 0119_disable_grok_fallback
Create Date: 2026-07-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0120_humanized_interaction_admission"
down_revision = "0119_disable_grok_fallback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_speaker_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("surface", sa.String(length=40), nullable=False, server_default="group_ai_chat"),
        sa.Column("conversation_key", sa.String(length=120), nullable=False),
        sa.Column("last_platform_account_id", sa.Integer(), nullable=True),
        sa.Column("last_platform_action_id", sa.String(length=36), nullable=True),
        sa.Column("last_platform_outcome", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("last_platform_content_source", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("last_human_cursor", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("reserved_account_id", sa.Integer(), nullable=True),
        sa.Column("reserved_action_id", sa.String(length=36), nullable=True),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "surface", "conversation_key", name="uq_conversation_speaker_state_key"),
    )
    op.create_index("ix_conversation_speaker_state_updated", "conversation_speaker_states", ["updated_at"])

    op.create_table(
        "conversation_speaker_turns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("surface", sa.String(length=40), nullable=False, server_default="group_ai_chat"),
        sa.Column("conversation_key", sa.String(length=120), nullable=False),
        sa.Column("remote_message_id", sa.String(length=160), nullable=False),
        sa.Column("remote_cursor", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("sender_kind", sa.String(length=40), nullable=False, server_default="platform"),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("action_id", sa.String(length=36), nullable=True),
        sa.Column("outcome", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("content_source", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("content_preview", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "surface",
            "conversation_key",
            "remote_message_id",
            name="uq_conversation_speaker_turn_remote",
        ),
    )
    op.create_index(
        "ix_conversation_speaker_turn_lookup",
        "conversation_speaker_turns",
        ["tenant_id", "surface", "conversation_key", "observed_at"],
    )

    op.create_table(
        "group_bot_admission_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("trusted_bot_peer_id", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("completion_policy", sa.String(length=40), nullable=False),
        sa.Column("evidence_ref", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("policy_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("revoked_by", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_group_bot_policy_group",
        "group_bot_admission_policies",
        ["tenant_id", "group_id", "status"],
    )
    op.create_index(
        "ix_group_bot_policy_bot",
        "group_bot_admission_policies",
        ["tenant_id", "group_id", "trusted_bot_peer_id", "status"],
    )
    # Partial unique: one active not_required per group; one active follow_sufficient per group+bot.
    op.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_group_bot_policy_active_not_required
            ON group_bot_admission_policies (tenant_id, group_id)
            WHERE status = 'active' AND completion_policy = 'not_required'
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_group_bot_policy_active_follow_sufficient
            ON group_bot_admission_policies (tenant_id, group_id, trusted_bot_peer_id)
            WHERE status = 'active' AND completion_policy = 'follow_sufficient'
            """
        )
    )

    op.create_table(
        "group_bot_admissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("membership_action_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("state", sa.String(length=60), nullable=False, server_default="awaiting_group_bot_rule"),
        sa.Column("completion_policy", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("policy_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("admission_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("trusted_bot_peer_id", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("join_start_cursor", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("observed_end_cursor", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("source_message_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("confirmation_message_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("transport_observation", sa.JSON(), nullable=False),
        sa.Column("post_send_visibility_state", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("required_channel_refs", sa.JSON(), nullable=False),
        sa.Column("failure_code", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("evidence_ref", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("abandoned_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("abandoned_by", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("join_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observation_closes_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "group_id", "account_id", name="uq_group_bot_admission_account"),
    )
    op.create_index(
        "ix_group_bot_admission_state",
        "group_bot_admissions",
        ["tenant_id", "group_id", "state"],
    )

    op.create_table(
        "group_bot_required_channel_follows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("admission_id", sa.Integer(), sa.ForeignKey("group_bot_admissions.id"), nullable=False),
        sa.Column("channel_ref", sa.String(length=255), nullable=False),
        sa.Column("source_message_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("action_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("resolved_peer_id", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("resolved_type", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="pending"),
        sa.Column("failure_code", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("admission_id", "channel_ref", name="uq_group_bot_required_channel_follow"),
    )

    op.create_table(
        "group_bot_admission_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("admission_id", sa.Integer(), sa.ForeignKey("group_bot_admissions.id"), nullable=False),
        sa.Column("join_start_cursor", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("observed_end_cursor", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("listener_account_id", sa.Integer(), nullable=True),
        sa.Column("read_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cursor_gap", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("failure_code", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("observation_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("result_summary", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "pending_visibility_credits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("bucket_id", sa.Integer(), nullable=True),
        sa.Column("action_id", sa.String(length=36), sa.ForeignKey("actions.id"), nullable=False),
        sa.Column("execution_attempt_id", sa.String(length=36), nullable=True),
        sa.Column("remote_message_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("hold_reason", sa.String(length=40), nullable=False, server_default="pending_visibility"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("action_id", name="uq_pending_visibility_credit_action"),
    )

    op.create_index(
        "ix_group_bot_admission_version",
        "group_bot_admissions",
        ["tenant_id", "group_id", "account_id", "admission_version"],
    )
    op.create_index(
        "ix_group_bot_channel_follow_status",
        "group_bot_required_channel_follows",
        ["admission_id", "status"],
    )
    op.create_index(
        "ix_group_bot_observation_admission",
        "group_bot_admission_observations",
        ["admission_id", "created_at"],
    )
    op.create_index(
        "ix_pending_visibility_credit_bucket",
        "pending_visibility_credits",
        ["bucket_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_visibility_credit_bucket", table_name="pending_visibility_credits")
    op.drop_index("ix_group_bot_observation_admission", table_name="group_bot_admission_observations")
    op.drop_index("ix_group_bot_channel_follow_status", table_name="group_bot_required_channel_follows")
    op.drop_index("ix_group_bot_admission_version", table_name="group_bot_admissions")
    op.drop_table("pending_visibility_credits")
    op.drop_table("group_bot_admission_observations")
    op.drop_table("group_bot_required_channel_follows")
    op.drop_table("group_bot_admissions")
    op.execute(sa.text("DROP INDEX IF EXISTS uq_group_bot_policy_active_follow_sufficient"))
    op.execute(sa.text("DROP INDEX IF EXISTS uq_group_bot_policy_active_not_required"))
    op.drop_table("group_bot_admission_policies")
    op.drop_index("ix_conversation_speaker_turn_lookup", table_name="conversation_speaker_turns")
    op.drop_table("conversation_speaker_turns")
    op.drop_index("ix_conversation_speaker_state_updated", table_name="conversation_speaker_states")
    op.drop_table("conversation_speaker_states")
