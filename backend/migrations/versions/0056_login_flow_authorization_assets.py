"""add authorization asset fields to login flows

Revision ID: 0056_login_flow_authorization_assets
Revises: 0055_account_authorizations
Create Date: 2026-06-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0056_login_flow_authorization_assets"
down_revision = "0055_account_authorizations"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(table_name)
    return any(column["name"] == column_name for column in columns)


def upgrade() -> None:
    if not _column_exists("tg_login_flows", "authorization_role"):
        op.add_column("tg_login_flows", sa.Column("authorization_role", sa.String(length=24), nullable=False, server_default="primary"))
    if not _column_exists("tg_login_flows", "authorization_id"):
        op.add_column("tg_login_flows", sa.Column("authorization_id", sa.Integer(), nullable=True))
    if not _column_exists("tg_login_flows", "developer_app_id"):
        op.add_column("tg_login_flows", sa.Column("developer_app_id", sa.Integer(), nullable=True))
    if not _column_exists("tg_login_flows", "proxy_id"):
        op.add_column("tg_login_flows", sa.Column("proxy_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    for column_name in ("proxy_id", "developer_app_id", "authorization_id", "authorization_role"):
        if _column_exists("tg_login_flows", column_name):
            op.drop_column("tg_login_flows", column_name)
