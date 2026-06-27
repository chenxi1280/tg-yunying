"""add tenant bot settings

Revision ID: 0067_tenant_bot_settings
Revises: 0066_ai_group_hard_target_10
Create Date: 2026-06-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0067_tenant_bot_settings"
down_revision = "0066_ai_group_hard_target_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _add_column_if_missing("tenants", sa.Column("telegram_bot_webhook_secret", sa.String(length=80), nullable=False, server_default=""))
    _add_column_if_missing("tenants", sa.Column("telegram_bot_webhook_status", sa.String(length=40), nullable=False, server_default="not_configured"))
    _add_column_if_missing("tenants", sa.Column("telegram_bot_last_error", sa.Text(), nullable=False, server_default=""))
    _add_column_if_missing("tenants", sa.Column("ai_group_bot_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    _create_conversation_table_if_missing()


def downgrade() -> None:
    _drop_table_if_exists("telegram_bot_conversations")
    _drop_column_if_exists("tenants", "ai_group_bot_enabled")
    _drop_column_if_exists("tenants", "telegram_bot_last_error")
    _drop_column_if_exists("tenants", "telegram_bot_webhook_status")
    _drop_column_if_exists("tenants", "telegram_bot_webhook_secret")


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


def _create_conversation_table_if_missing() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("telegram_bot_conversations"):
        return
    op.create_table(
        "telegram_bot_conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("chat_id", sa.String(length=120), nullable=False),
        sa.Column("task_id", sa.String(length=80), nullable=False),
        sa.Column("step", sa.String(length=40), nullable=False),
        sa.Column("draft_config", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("tenant_id", "chat_id"),
    )


def _drop_table_if_exists(table_name: str) -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(table_name):
        op.drop_table(table_name)
