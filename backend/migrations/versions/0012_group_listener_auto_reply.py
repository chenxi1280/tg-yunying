"""group listener auto reply

Revision ID: 0012_group_listener_auto_reply
Revises: 0011_activation_code_management
Create Date: 2026-05-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_group_listener_auto_reply"
down_revision = "0011_activation_code_management"
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
    _add_column_if_missing("tg_group_accounts", sa.Column("is_listener", sa.Boolean(), nullable=False, server_default=sa.false()))
    for column in [
        sa.Column("listener_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("listener_interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("listener_context_limit", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("listener_auto_reply_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("listener_last_polled_at", sa.DateTime(), nullable=True),
        sa.Column("listener_last_reply_at", sa.DateTime(), nullable=True),
        sa.Column("listener_last_error", sa.Text(), nullable=False, server_default=""),
    ]:
        _add_column_if_missing("tg_groups", column)

    if not _table_exists("group_context_messages"):
        op.create_table(
            "group_context_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("group_id", sa.Integer(), sa.ForeignKey("tg_groups.id"), nullable=False),
            sa.Column("listener_account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("sender_peer_id", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("sender_name", sa.String(length=160), nullable=False, server_default="真人用户"),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("message_type", sa.String(length=40), nullable=False, server_default="text"),
            sa.Column("remote_message_id", sa.String(length=160), nullable=False),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("used_for_ai", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("group_id", "remote_message_id"),
        )
    op.create_index("ix_group_context_messages_group_used", "group_context_messages", ["group_id", "used_for_ai"], if_not_exists=True)
    op.create_index("ix_group_context_messages_sent_at", "group_context_messages", ["sent_at"], if_not_exists=True)


def downgrade() -> None:
    if _table_exists("group_context_messages"):
        op.drop_index("ix_group_context_messages_sent_at", table_name="group_context_messages", if_exists=True)
        op.drop_index("ix_group_context_messages_group_used", table_name="group_context_messages", if_exists=True)
        op.drop_table("group_context_messages")
    for name in [
        "listener_last_error",
        "listener_last_reply_at",
        "listener_last_polled_at",
        "listener_auto_reply_enabled",
        "listener_context_limit",
        "listener_interval_seconds",
        "listener_enabled",
    ]:
        if _column_exists("tg_groups", name):
            op.drop_column("tg_groups", name)
    if _column_exists("tg_group_accounts", "is_listener"):
        op.drop_column("tg_group_accounts", "is_listener")
