"""Persist safe inline group-bot control button summaries.

Revision ID: 0125_group_bot_controls
Revises: 0124_voice_profile_generation
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0125_group_bot_controls"
down_revision = "0124_voice_profile_generation"
branch_labels = None
depends_on = None

TABLE = "group_context_messages"
COLUMN = "control_buttons"


def upgrade() -> None:
    if not _has_column(TABLE, COLUMN):
        op.add_column(
            TABLE,
            sa.Column(COLUMN, sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        )


def downgrade() -> None:
    if _has_column(TABLE, COLUMN):
        op.drop_column(TABLE, COLUMN)


def _has_column(table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
