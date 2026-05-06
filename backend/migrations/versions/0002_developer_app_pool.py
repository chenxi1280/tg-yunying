"""developer app pool

Revision ID: 0002_developer_app_pool
Revises: 0001_initial
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002_developer_app_pool"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(table_name)


def _columns(table_name: str) -> set[str]:
    if not _table_exists(table_name):
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _fk_exists(table_name: str, constrained_columns: list[str], referred_table: str) -> bool:
    if not _table_exists(table_name):
        return False
    for fk in sa.inspect(op.get_bind()).get_foreign_keys(table_name):
        if fk.get("constrained_columns") == constrained_columns and fk.get("referred_table") == referred_table:
            return True
    return False


def upgrade() -> None:
    if not _table_exists("telegram_developer_apps"):
        op.create_table(
            "telegram_developer_apps",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("app_name", sa.String(length=100), nullable=False),
            sa.Column("api_id", sa.Integer(), nullable=False, unique=True),
            sa.Column("api_hash_ciphertext", sa.Text(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("health_status", sa.String(length=30), nullable=False, server_default="健康"),
            sa.Column("max_accounts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("credentials_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("last_assigned_at", sa.DateTime(), nullable=True),
            sa.Column("last_check_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("notes", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
    account_columns = _columns("tg_accounts")
    if "phone_ciphertext" not in account_columns:
        op.add_column("tg_accounts", sa.Column("phone_ciphertext", sa.Text(), nullable=True))
    if "developer_app_id" not in account_columns:
        op.add_column("tg_accounts", sa.Column("developer_app_id", sa.Integer(), nullable=True))
    if "developer_app_version" not in account_columns:
        op.add_column("tg_accounts", sa.Column("developer_app_version", sa.Integer(), nullable=False, server_default="1"))
    if not _fk_exists("tg_accounts", ["developer_app_id"], "telegram_developer_apps"):
        op.create_foreign_key(
            "fk_tg_accounts_developer_app_id",
            "tg_accounts",
            "telegram_developer_apps",
            ["developer_app_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    op.drop_constraint("fk_tg_accounts_developer_app_id", "tg_accounts", type_="foreignkey")
    op.drop_column("tg_accounts", "developer_app_version")
    op.drop_column("tg_accounts", "developer_app_id")
    op.drop_column("tg_accounts", "phone_ciphertext")
    op.drop_table("telegram_developer_apps")
