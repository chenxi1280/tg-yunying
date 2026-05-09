"""campaign backoff scheduling fields

Revision ID: 0020_campaign_backoff
Revises: 0019_ai_notifications
Create Date: 2026-05-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0020_campaign_backoff"
down_revision = "0019_ai_notifications"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_exists(table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(_bind()).get_columns(table)}


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if _table_exists(table) and not _column_exists(table, column.name):
        op.add_column(table, column)


def upgrade() -> None:
    _add_column_if_missing("campaigns", sa.Column("next_run_at", sa.DateTime(), nullable=True))
    _add_column_if_missing(
        "campaigns",
        sa.Column("consecutive_failure_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    if _table_exists("campaigns"):
        for name in ["consecutive_failure_count", "next_run_at"]:
            if _column_exists("campaigns", name):
                op.drop_column("campaigns", name)
