"""repair scheduling account limits

Revision ID: 0030_repair_sched_limits
Revises: 0029_channel_message_comments
Create Date: 2026-05-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0030_repair_sched_limits"
down_revision = "0029_channel_message_comments"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    table = "scheduling_settings"
    _add_column_if_missing(table, sa.Column("default_account_hour_limit", sa.Integer(), nullable=False, server_default="0"))
    _add_column_if_missing(table, sa.Column("default_account_day_limit", sa.Integer(), nullable=False, server_default="0"))
    _add_column_if_missing(table, sa.Column("default_account_cooldown_seconds", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    columns = _columns("scheduling_settings")
    for name in [
        "default_account_cooldown_seconds",
        "default_account_day_limit",
        "default_account_hour_limit",
    ]:
        if name in columns:
            op.drop_column("scheduling_settings", name)
