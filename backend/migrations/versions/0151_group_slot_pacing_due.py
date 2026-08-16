"""Repair the missing group-message pacing due timestamp.

Revision ID: 0151_group_slot_pacing_due
Revises: 0150_pacing_slot_fields
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0151_group_slot_pacing_due"
down_revision = "0150_pacing_slot_fields"
branch_labels = None
depends_on = None

TABLE_NAME = "task_group_daily_message_slots"
COLUMN_NAME = "pacing_due_at"


def _column_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE_NAME):
        return set()
    return {str(column["name"]) for column in inspector.get_columns(TABLE_NAME)}


def upgrade() -> None:
    if COLUMN_NAME not in _column_names():
        op.add_column(
            TABLE_NAME,
            sa.Column(COLUMN_NAME, sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    if COLUMN_NAME in _column_names():
        op.drop_column(TABLE_NAME, COLUMN_NAME)
