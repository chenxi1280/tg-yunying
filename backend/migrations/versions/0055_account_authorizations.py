"""add account authorization assets

Revision ID: 0055_account_authorizations
Revises: 0054_comment_availability
Create Date: 2026-06-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0055_account_authorizations"
down_revision = "0054_comment_availability"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def upgrade() -> None:
    if _table_exists("tg_account_authorizations"):
        return
    op.create_table(
        "tg_account_authorizations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False, server_default="primary"),
        sa.Column("developer_app_id", sa.Integer(), sa.ForeignKey("telegram_developer_apps.id"), nullable=True),
        sa.Column("proxy_id", sa.Integer(), sa.ForeignKey("account_proxies.id"), nullable=True),
        sa.Column("session_ciphertext", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
        sa.Column("health_status", sa.String(length=30), nullable=False, server_default="unknown"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("telegram_authorization_hash_ciphertext", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_health_check_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_switched_at", sa.DateTime(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("disabled_at", sa.DateTime(), nullable=True),
        sa.Column("disabled_by", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tg_account_authorizations_account", "tg_account_authorizations", ["account_id", "role"])
    op.create_index(
        "ux_tg_account_authorizations_current",
        "tg_account_authorizations",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("is_current IS TRUE"),
    )


def downgrade() -> None:
    if not _table_exists("tg_account_authorizations"):
        return
    op.drop_index("ux_tg_account_authorizations_current", table_name="tg_account_authorizations")
    op.drop_index("ix_tg_account_authorizations_account", table_name="tg_account_authorizations")
    op.drop_table("tg_account_authorizations")
