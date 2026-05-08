"""drop legacy user subscription authorization tables

Revision ID: 0018_drop_legacy_authorization
Revises: 0017_operations_center
Create Date: 2026-05-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0018_drop_legacy_authorization"
down_revision = "0017_operations_center"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _inspector():
    return sa.inspect(_bind())


def _table_exists(name: str) -> bool:
    return _inspector().has_table(name)


def _column_exists(table: str, column: str) -> bool:
    if not _table_exists(table):
        return False
    return column in {item["name"] for item in _inspector().get_columns(table)}


def _drop_fks_to(table: str, referred_table: str) -> None:
    if not _table_exists(table):
        return
    for fk in _inspector().get_foreign_keys(table):
        if fk.get("referred_table") != referred_table:
            continue
        name = fk.get("name")
        if name:
            op.drop_constraint(name, table, type_="foreignkey")


def _drop_index_if_exists(table: str, index_name: str) -> None:
    if not _table_exists(table):
        return
    if index_name in {item["name"] for item in _inspector().get_indexes(table)}:
        op.drop_index(index_name, table_name=table)


def upgrade() -> None:
    _drop_fks_to("ai_usage_ledgers", "app_users")
    if _table_exists("ai_usage_ledgers") and _column_exists("ai_usage_ledgers", "user_id"):
        try:
            op.alter_column("ai_usage_ledgers", "user_id", existing_type=sa.Integer(), nullable=True)
        except Exception:
            pass
    _drop_index_if_exists("ai_usage_ledgers", "ix_ai_usage_ledgers_user_created_at")

    for table in ("user_token_ledgers", "activation_codes", "subscription_plans", "app_users"):
        if _table_exists(table):
            op.drop_table(table)


def downgrade() -> None:
    if not _table_exists("app_users"):
        op.create_table(
            "app_users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.Column("role", sa.String(length=40), nullable=False),
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
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("last_login_at", sa.DateTime(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
    if not _table_exists("subscription_plans"):
        op.create_table(
            "subscription_plans",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("plan_type", sa.String(length=30), nullable=False, unique=True),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.Column("duration_days", sa.Integer(), nullable=False),
            sa.Column("token_quota", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("note", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
    if not _table_exists("activation_codes"):
        op.create_table(
            "activation_codes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(length=64), nullable=False, unique=True),
            sa.Column("plan_id", sa.Integer(), sa.ForeignKey("subscription_plans.id"), nullable=True),
            sa.Column("plan_type", sa.String(length=30), nullable=False),
            sa.Column("plan_name", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("duration_days", sa.Integer(), nullable=False),
            sa.Column("token_quota", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="unused"),
            sa.Column("batch_no", sa.String(length=24), nullable=False, server_default=""),
            sa.Column("serial_prefix", sa.String(length=24), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("redeemed_by_user_id", sa.Integer(), sa.ForeignKey("app_users.id"), nullable=True),
            sa.Column("redeemed_at", sa.DateTime(), nullable=True),
            sa.Column("subscription_start_at", sa.DateTime(), nullable=True),
            sa.Column("subscription_end_at", sa.DateTime(), nullable=True),
            sa.Column("note", sa.String(length=255), nullable=False, server_default=""),
        )
    if not _table_exists("user_token_ledgers"):
        op.create_table(
            "user_token_ledgers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("app_users.id"), nullable=False),
            sa.Column("change_type", sa.String(length=40), nullable=False),
            sa.Column("delta_tokens", sa.Integer(), nullable=False),
            sa.Column("balance_after", sa.Integer(), nullable=False),
            sa.Column("related_activation_code_id", sa.Integer(), sa.ForeignKey("activation_codes.id"), nullable=True),
            sa.Column("related_ai_usage_ledger_id", sa.Integer(), sa.ForeignKey("ai_usage_ledgers.id"), nullable=True),
            sa.Column("reason", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("actor", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
