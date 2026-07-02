"""add account center prd contract fields

Revision ID: 0074_account_center_prd_contracts
Revises: 0073_account_mask_fields
Create Date: 2026-07-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0074_account_center_prd_contracts"
down_revision = "0073_account_mask_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _add_column_if_missing("tg_accounts", sa.Column("account_identity", sa.String(length=40), nullable=False, server_default="normal"))
    _add_column_if_missing("account_pools", sa.Column("pool_purpose", sa.String(length=40), nullable=False, server_default="normal"))
    _add_column_if_missing("account_pools", sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()))
    _add_column_if_missing("account_pools", sa.Column("system_key", sa.String(length=80), nullable=False, server_default=""))
    _add_column_if_missing("tg_account_authorizations", sa.Column("developer_app_api_id_snapshot", sa.Integer(), nullable=False, server_default="0"))
    _add_column_if_missing("tg_account_authorizations", sa.Column("derived_status", sa.String(length=40), nullable=False, server_default="unknown"))
    _add_column_if_missing("tg_account_security_batch_items", sa.Column("device_cleanup_precheck_id", sa.String(length=80), nullable=False, server_default=""))
    if not _has_table("tg_account_device_cleanup_prechecks"):
        op.create_table(
            "tg_account_device_cleanup_prechecks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("precheck_id", sa.String(length=80), nullable=False, unique=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("cleanup_authorization_hashes", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("cleanup_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("kept_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("unknown_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="ready"),
            sa.Column("created_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("confirmed_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    if _has_table("tg_account_device_cleanup_prechecks"):
        op.drop_table("tg_account_device_cleanup_prechecks")
    _drop_column_if_exists("tg_account_security_batch_items", "device_cleanup_precheck_id")
    _drop_column_if_exists("tg_account_authorizations", "derived_status")
    _drop_column_if_exists("tg_account_authorizations", "developer_app_api_id_snapshot")
    _drop_column_if_exists("account_pools", "system_key")
    _drop_column_if_exists("account_pools", "is_system")
    _drop_column_if_exists("account_pools", "pool_purpose")
    _drop_column_if_exists("tg_accounts", "account_identity")


def _column_names(table_name: str) -> set[str]:
    return {row["name"] for row in sa.inspect(op.get_bind()).get_columns(table_name)}


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _column_names(table_name):
        op.add_column(table_name, column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if column_name in _column_names(table_name):
        op.drop_column(table_name, column_name)
