"""scheduling runtime policy

Revision ID: 0026_scheduling_runtime_policy
Revises: 0025_archive_runtime_columns
Create Date: 2026-05-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0026_scheduling_runtime_policy"
down_revision = "0025_archive_runtime_columns"
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
    _add_column_if_missing(table, sa.Column("quiet_hours_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    _add_column_if_missing(table, sa.Column("quiet_start", sa.String(length=16), nullable=False, server_default="02:00"))
    _add_column_if_missing(table, sa.Column("quiet_end", sa.String(length=16), nullable=False, server_default="08:00"))
    _add_column_if_missing(table, sa.Column("quiet_timezone", sa.String(length=64), nullable=False, server_default="Asia/Shanghai"))
    _add_column_if_missing(table, sa.Column("default_max_retries", sa.Integer(), nullable=False, server_default="3"))
    _add_column_if_missing(table, sa.Column("default_retry_delay_seconds", sa.Integer(), nullable=False, server_default="60"))
    _add_column_if_missing(table, sa.Column("default_retry_backoff", sa.String(length=20), nullable=False, server_default="exponential"))
    _add_column_if_missing(table, sa.Column("default_on_account_banned", sa.String(length=30), nullable=False, server_default="skip_account"))
    _add_column_if_missing(table, sa.Column("default_on_api_rate_limit", sa.String(length=30), nullable=False, server_default="wait_and_retry"))
    _add_column_if_missing(table, sa.Column("default_on_content_rejected", sa.String(length=30), nullable=False, server_default="skip_message"))
    _add_column_if_missing(table, sa.Column("default_account_hour_limit", sa.Integer(), nullable=False, server_default="0"))
    _add_column_if_missing(table, sa.Column("default_account_day_limit", sa.Integer(), nullable=False, server_default="0"))
    _add_column_if_missing(table, sa.Column("default_account_cooldown_seconds", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    for name in [
        "default_on_content_rejected",
        "default_account_cooldown_seconds",
        "default_account_day_limit",
        "default_account_hour_limit",
        "default_on_api_rate_limit",
        "default_on_account_banned",
        "default_retry_backoff",
        "default_retry_delay_seconds",
        "default_max_retries",
        "quiet_timezone",
        "quiet_end",
        "quiet_start",
        "quiet_hours_enabled",
    ]:
        if name in _columns("scheduling_settings"):
            op.drop_column("scheduling_settings", name)
