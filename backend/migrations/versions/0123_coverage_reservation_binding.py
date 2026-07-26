"""Bind a coverage reservation after its action exists.

Revision ID: 0123_coverage_reservation
Revises: 0122_claim_protocol_trace
Create Date: 2026-07-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0123_coverage_reservation"
down_revision = "0122_claim_protocol_trace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if _has_column("task_account_daily_coverage", "reservation_token"):
        return
    op.add_column(
        "task_account_daily_coverage",
        sa.Column("reservation_token", sa.String(length=36), nullable=True),
    )


def downgrade() -> None:
    if _has_column("task_account_daily_coverage", "reservation_token"):
        op.drop_column("task_account_daily_coverage", "reservation_token")


def _has_column(table: str, column: str) -> bool:
    if table not in sa.inspect(op.get_bind()).get_table_names():
        return False
    return column in {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
