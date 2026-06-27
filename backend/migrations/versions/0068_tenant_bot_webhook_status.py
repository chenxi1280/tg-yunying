"""tenant bot webhook status fields

Revision ID: 0068_tenant_bot_webhook_status
Revises: 0067_tenant_bot_settings
Create Date: 2026-06-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0068_tenant_bot_webhook_status"
down_revision = "0067_tenant_bot_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _add_column_if_missing("tenants", sa.Column("telegram_bot_webhook_current_url", sa.Text(), nullable=False, server_default=""))
    _add_column_if_missing("tenants", sa.Column("telegram_bot_webhook_last_checked_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    _drop_column_if_exists("tenants", "telegram_bot_webhook_last_checked_at")
    _drop_column_if_exists("tenants", "telegram_bot_webhook_current_url")


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    bind = op.get_bind()
    existing = {row["name"] for row in sa.inspect(bind).get_columns(table_name)}
    if column.name not in existing:
        op.add_column(table_name, column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    bind = op.get_bind()
    existing = {row["name"] for row in sa.inspect(bind).get_columns(table_name)}
    if column_name in existing:
        op.drop_column(table_name, column_name)
