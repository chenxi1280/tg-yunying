"""channel message comments

Revision ID: 0029_channel_message_comments
Revises: 0028_worker_heartbeats
Create Date: 2026-05-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0029_channel_message_comments"
down_revision = "0028_worker_heartbeats"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _table_exists("channel_message_comments"):
        return
    op.create_table(
        "channel_message_comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel_target_id", sa.Integer(), nullable=False),
        sa.Column("channel_message_id", sa.Integer(), nullable=False),
        sa.Column("comment_message_id", sa.Integer(), nullable=False),
        sa.Column("parent_comment_message_id", sa.Integer(), nullable=True),
        sa.Column("author_peer_id", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("author_name", sa.String(length=180), nullable=False, server_default=""),
        sa.Column("content_preview", sa.Text(), nullable=False, server_default=""),
        sa.Column("reply_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["channel_message_id"], ["channel_messages.id"]),
        sa.ForeignKeyConstraint(["channel_target_id"], ["operation_targets.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "channel_target_id", "channel_message_id", "comment_message_id"),
    )


def downgrade() -> None:
    if _table_exists("channel_message_comments"):
        op.drop_table("channel_message_comments")
