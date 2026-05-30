"""track channel message comment availability

Revision ID: 0054_comment_availability
Revises: 0053_tenant_learning_profiles
Create Date: 2026-05-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0054_comment_availability"
down_revision = "0053_tenant_learning_profiles"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in sa.inspect(op.get_bind()).get_columns(table_name))


def upgrade() -> None:
    if _column_exists("channel_messages", "comment_available"):
        return
    op.add_column("channel_messages", sa.Column("comment_available", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    if _column_exists("channel_messages", "comment_available"):
        op.drop_column("channel_messages", "comment_available")
