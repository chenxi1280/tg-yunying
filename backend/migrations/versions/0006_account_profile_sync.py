"""account profile fields and sync records

Revision ID: 0006_profile_sync
Revises: 0005_dialogue_assignment
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0006_profile_sync"
down_revision = "0005_dialogue_assignment"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _columns(table_name: str) -> set[str]:
    if not _table_exists(table_name):
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    _add_column_if_missing("tg_accounts", sa.Column("tg_first_name", sa.String(length=80), nullable=False, server_default=""))
    _add_column_if_missing("tg_accounts", sa.Column("tg_last_name", sa.String(length=80), nullable=False, server_default=""))
    _add_column_if_missing("tg_accounts", sa.Column("tg_bio", sa.Text(), nullable=False, server_default=""))
    _add_column_if_missing("tg_accounts", sa.Column("avatar_object_key", sa.String(length=300), nullable=False, server_default=""))
    _add_column_if_missing("tg_accounts", sa.Column("profile_sync_status", sa.String(length=30), nullable=False, server_default="未同步"))
    _add_column_if_missing("tg_accounts", sa.Column("profile_sync_error", sa.Text(), nullable=False, server_default=""))
    _add_column_if_missing("tg_accounts", sa.Column("profile_synced_at", sa.DateTime(), nullable=True))

    if not _table_exists("tg_account_profile_sync_records"):
        op.create_table(
            "tg_account_profile_sync_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("actor", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("before_snapshot", sa.Text(), nullable=False, server_default=""),
            sa.Column("after_snapshot", sa.Text(), nullable=False, server_default=""),
            sa.Column("avatar_object_key", sa.String(length=300), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="排队中"),
            sa.Column("failure_type", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("remote_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("synced_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    if _table_exists("tg_account_profile_sync_records"):
        op.drop_table("tg_account_profile_sync_records")
    for name in [
        "profile_synced_at",
        "profile_sync_error",
        "profile_sync_status",
        "avatar_object_key",
        "tg_bio",
        "tg_last_name",
        "tg_first_name",
    ]:
        if name in _columns("tg_accounts"):
            op.drop_column("tg_accounts", name)
