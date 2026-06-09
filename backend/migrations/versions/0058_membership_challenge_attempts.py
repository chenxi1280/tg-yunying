"""membership challenge attempts

Revision ID: 0058_membership_challenge_attempts
Revises: 0057_ai_group_hard_target_300
Create Date: 2026-06-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0058_membership_challenge_attempts"
down_revision = "0057_ai_group_hard_target_300"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "target_membership_challenge_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("verification_task_id", sa.Integer(), sa.ForeignKey("verification_tasks.id"), nullable=True),
        sa.Column("membership_item_id", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("tg_groups.id"), nullable=True),
        sa.Column("challenge_type", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("question_hash", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("question_snapshot", sa.Text(), nullable=False, server_default=""),
        sa.Column("context_status", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("context_message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("context_failure_detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("media_message_id", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("media_fingerprint", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("media_mime_type", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("answer_source", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("answer_text", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("model_name", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("result_snapshot", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(length=120), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_membership_challenge_attempt_identity",
        "target_membership_challenge_attempts",
        ["tenant_id", "verification_task_id", "question_hash", "media_fingerprint"],
    )


def downgrade() -> None:
    op.drop_index("ix_membership_challenge_attempt_identity", table_name="target_membership_challenge_attempts")
    op.drop_table("target_membership_challenge_attempts")
