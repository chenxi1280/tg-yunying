"""account sync records

Revision ID: 0009_account_sync_records
Revises: 0008_account_ops
Create Date: 2026-05-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_account_sync_records"
down_revision = "0008_account_ops"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def upgrade() -> None:
    if not _table_exists("tg_account_sync_records"):
        op.create_table(
            "tg_account_sync_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("sync_type", sa.String(length=40), nullable=False),
            sa.Column("trigger_source", sa.String(length=60), nullable=False, server_default="manual"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="排队中"),
            sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failure_type", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("scheduled_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    if _table_exists("tg_account_sync_records"):
        op.drop_table("tg_account_sync_records")
