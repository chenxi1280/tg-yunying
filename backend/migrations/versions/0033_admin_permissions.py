"""admin permissions

Revision ID: 0033_admin_permissions
Revises: 0032_risk_control_proxy_center
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0033_admin_permissions"
down_revision = "0032_risk_control_proxy_center"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_names(table: str) -> set[str]:
    if not _table_exists(table):
        return set()
    return {item["name"] for item in sa.inspect(_bind()).get_columns(table)}


def upgrade() -> None:
    if not _table_exists("app_users"):
        op.create_table(
            "app_users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.Column("role", sa.String(length=40), nullable=False, server_default="后台用户"),
            sa.Column("role_template", sa.String(length=40), nullable=False, server_default="运营管理员"),
            sa.Column("email", sa.String(length=160), nullable=False, unique=True),
            sa.Column("phone", sa.String(length=40), nullable=True, unique=True),
            sa.Column("password_hash", sa.String(length=240), nullable=False, server_default=""),
            sa.Column("subscription_status", sa.String(length=30), nullable=False, server_default="active"),
            sa.Column("subscription_started_at", sa.DateTime(), nullable=True),
            sa.Column("subscription_expires_at", sa.DateTime(), nullable=True),
            sa.Column("last_activated_at", sa.DateTime(), nullable=True),
            sa.Column("token_balance", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("token_quota_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("menu_permissions", sa.Text(), nullable=False, server_default=""),
            sa.Column("permission_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("last_login_at", sa.DateTime(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
        op.create_index("ix_app_users_email", "app_users", ["email"], unique=True, if_not_exists=True)
        op.create_index("ix_app_users_phone", "app_users", ["phone"], unique=True, if_not_exists=True)
    if not _table_exists("user_token_ledgers"):
        op.create_table(
            "user_token_ledgers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("app_users.id"), nullable=False),
            sa.Column("change_type", sa.String(length=40), nullable=False),
            sa.Column("delta_tokens", sa.Integer(), nullable=False),
            sa.Column("balance_after", sa.Integer(), nullable=False),
            sa.Column("related_activation_code_id", sa.Integer(), nullable=True),
            sa.Column("related_ai_usage_ledger_id", sa.Integer(), nullable=True),
            sa.Column("reason", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("actor", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_user_token_ledgers_user_created", "user_token_ledgers", ["user_id", "created_at"], if_not_exists=True)
    columns = _column_names("app_users")
    if "role_template" not in columns:
        op.add_column("app_users", sa.Column("role_template", sa.String(40), nullable=False, server_default="运营管理员"))
    if "permission_version" not in columns:
        op.add_column("app_users", sa.Column("permission_version", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    if not _table_exists("app_users"):
        return
    if _table_exists("user_token_ledgers"):
        op.drop_index("ix_user_token_ledgers_user_created", table_name="user_token_ledgers", if_exists=True)
        op.drop_table("user_token_ledgers")
    columns = _column_names("app_users")
    if "permission_version" in columns:
        op.drop_column("app_users", "permission_version")
    if "role_template" in columns:
        op.drop_column("app_users", "role_template")
