"""channel comment author identity"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0052_comment_author_identity"
down_revision = "0051_target_learning_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("channel_message_comments")}
    if "author_username" not in columns:
        op.add_column("channel_message_comments", sa.Column("author_username", sa.String(length=120), nullable=False, server_default=""))
        op.alter_column("channel_message_comments", "author_username", server_default=None)
    if "is_bot" not in columns:
        op.add_column("channel_message_comments", sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.false()))
        op.alter_column("channel_message_comments", "is_bot", server_default=None)


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("channel_message_comments")}
    if "is_bot" in columns:
        op.drop_column("channel_message_comments", "is_bot")
    if "author_username" in columns:
        op.drop_column("channel_message_comments", "author_username")
