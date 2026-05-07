"""account detail, verification codes, and private message tasks

Revision ID: 0004_account_detail_tasks
Revises: 0003_ai_config_scheduling
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0004_account_detail_tasks"
down_revision = "0003_ai_config_scheduling"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _columns(table_name: str) -> dict[str, dict]:
    if not _table_exists(table_name):
        return {}
    return {column["name"]: column for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def _make_nullable(table_name: str, column_name: str) -> None:
    column = _columns(table_name).get(column_name)
    if not column or column.get("nullable") is True:
        return
    op.alter_column(table_name, column_name, existing_type=column["type"], nullable=True)


def upgrade() -> None:
    if not _table_exists("tg_verification_codes"):
        op.create_table(
            "tg_verification_codes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("source", sa.String(length=40), nullable=False, server_default="telegram_service_message"),
            sa.Column("code_preview", sa.String(length=24), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("viewed_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("viewed_at", sa.DateTime(), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="可查看"),
            sa.Column("raw_hint", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )

    _add_column_if_missing("campaigns", sa.Column("target_group_ids", sa.Text(), nullable=False, server_default=""))
    _add_column_if_missing("campaigns", sa.Column("selected_account_ids_by_group", sa.Text(), nullable=False, server_default=""))

    _add_column_if_missing("message_tasks", sa.Column("target_type", sa.String(length=30), nullable=False, server_default="group"))
    _add_column_if_missing("message_tasks", sa.Column("target_peer_id", sa.String(length=120), nullable=True))
    _add_column_if_missing("message_tasks", sa.Column("target_display", sa.String(length=160), nullable=False, server_default=""))
    _make_nullable("message_tasks", "campaign_id")
    _make_nullable("message_tasks", "group_id")


def downgrade() -> None:
    _make_nullable("message_tasks", "campaign_id")
    _make_nullable("message_tasks", "group_id")
    for table, names in {
        "message_tasks": ["target_display", "target_peer_id", "target_type"],
        "campaigns": ["selected_account_ids_by_group", "target_group_ids"],
    }.items():
        existing = _columns(table)
        for name in names:
            if name in existing:
                op.drop_column(table, name)
    if _table_exists("tg_verification_codes"):
        op.drop_table("tg_verification_codes")
