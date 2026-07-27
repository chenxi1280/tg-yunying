"""persist the next legacy group send claim slot

Revision ID: 0127_group_send_claim_slot
Revises: 0126_coverage_terminal_unknown
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0127_group_send_claim_slot"
down_revision = "0126_coverage_terminal_unknown"
branch_labels = None
depends_on = None

TABLE = "tg_groups"
COLUMN = "next_group_send_slot_at"


def upgrade() -> None:
    if not _has_column():
        op.add_column(TABLE, sa.Column(COLUMN, sa.DateTime(), nullable=True))


def downgrade() -> None:
    if _has_column():
        op.drop_column(TABLE, COLUMN)


def _has_column() -> bool:
    return COLUMN in {item["name"] for item in sa.inspect(op.get_bind()).get_columns(TABLE)}
