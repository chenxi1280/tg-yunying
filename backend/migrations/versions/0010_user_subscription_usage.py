"""user subscription activation codes and ai usage ledgers

Revision ID: 0010_user_subscription_usage
Revises: 0009_account_sync_records
Create Date: 2026-05-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_user_subscription_usage"
down_revision = "0009_account_sync_records"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_exists(table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(_bind()).get_columns(table)}


def upgrade() -> None:
    if _table_exists("app_users"):
        additions = [
            ("phone", sa.Column("phone", sa.String(length=40), nullable=True)),
            ("subscription_status", sa.Column("subscription_status", sa.String(length=30), nullable=False, server_default="active")),
            ("subscription_started_at", sa.Column("subscription_started_at", sa.DateTime(), nullable=True)),
            ("subscription_expires_at", sa.Column("subscription_expires_at", sa.DateTime(), nullable=True)),
            ("last_activated_at", sa.Column("last_activated_at", sa.DateTime(), nullable=True)),
            ("created_at", sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"))),
            ("last_login_at", sa.Column("last_login_at", sa.DateTime(), nullable=True)),
        ]
        for name, column in additions:
            if not _column_exists("app_users", name):
                op.add_column("app_users", column)
        op.execute(
            sa.text(
                """
                UPDATE app_users
                SET role = CASE
                    WHEN role = '平台管理员' THEN '系统管理员'
                    ELSE '普通用户'
                END
                """
            )
        )
        op.execute(
            sa.text(
                """
                UPDATE app_users
                SET subscription_status = CASE
                    WHEN role = '系统管理员' THEN 'active'
                    ELSE COALESCE(subscription_status, 'pending_activation')
                END
                """
            )
        )
        op.create_index("ix_app_users_phone", "app_users", ["phone"], unique=True, if_not_exists=True)

    if _table_exists("ai_providers"):
        additions = [
            ("input_price_per_1k", sa.Column("input_price_per_1k", sa.Float(), nullable=False, server_default="0")),
            ("output_price_per_1k", sa.Column("output_price_per_1k", sa.Float(), nullable=False, server_default="0")),
            ("currency", sa.Column("currency", sa.String(length=16), nullable=False, server_default="CNY")),
            ("is_billable", sa.Column("is_billable", sa.Boolean(), nullable=False, server_default=sa.true())),
        ]
        for name, column in additions:
            if not _column_exists("ai_providers", name):
                op.add_column("ai_providers", column)

    if not _table_exists("activation_codes"):
        op.create_table(
            "activation_codes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(length=64), nullable=False, unique=True),
            sa.Column("plan_type", sa.String(length=30), nullable=False),
            sa.Column("duration_days", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="unused"),
            sa.Column("created_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("redeemed_by_user_id", sa.Integer(), sa.ForeignKey("app_users.id"), nullable=True),
            sa.Column("redeemed_at", sa.DateTime(), nullable=True),
            sa.Column("subscription_start_at", sa.DateTime(), nullable=True),
            sa.Column("subscription_end_at", sa.DateTime(), nullable=True),
            sa.Column("note", sa.String(length=255), nullable=False, server_default=""),
        )

    if not _table_exists("ai_usage_ledgers"):
        op.create_table(
            "ai_usage_ledgers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("app_users.id"), nullable=False),
            sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), nullable=True),
            sa.Column("group_id", sa.Integer(), sa.ForeignKey("tg_groups.id"), nullable=True),
            sa.Column("provider_id", sa.Integer(), sa.ForeignKey("ai_providers.id"), nullable=True),
            sa.Column("provider_name", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("model_name", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("prompt_template_id", sa.Integer(), sa.ForeignKey("prompt_templates.id"), nullable=True),
            sa.Column("request_type", sa.String(length=60), nullable=False, server_default="campaign_draft_generation"),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("input_unit_price", sa.Float(), nullable=False, server_default="0"),
            sa.Column("output_unit_price", sa.Float(), nullable=False, server_default="0"),
            sa.Column("total_cost", sa.Float(), nullable=False, server_default="0"),
            sa.Column("currency", sa.String(length=16), nullable=False, server_default="CNY"),
            sa.Column("billable", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("request_status", sa.String(length=30), nullable=False, server_default="success"),
            sa.Column("error_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_ai_usage_ledgers_user_created_at", "ai_usage_ledgers", ["user_id", "created_at"])
        op.create_index("ix_ai_usage_ledgers_campaign", "ai_usage_ledgers", ["campaign_id"])


def downgrade() -> None:
    if _table_exists("ai_usage_ledgers"):
        op.drop_index("ix_ai_usage_ledgers_campaign", table_name="ai_usage_ledgers")
        op.drop_index("ix_ai_usage_ledgers_user_created_at", table_name="ai_usage_ledgers")
        op.drop_table("ai_usage_ledgers")
    if _table_exists("activation_codes"):
        op.drop_table("activation_codes")
    if _table_exists("ai_providers"):
        for name in ["is_billable", "currency", "output_price_per_1k", "input_price_per_1k"]:
            if _column_exists("ai_providers", name):
                op.drop_column("ai_providers", name)
    if _table_exists("app_users"):
        op.drop_index("ix_app_users_phone", table_name="app_users", if_exists=True)
        for name in [
            "last_login_at",
            "created_at",
            "last_activated_at",
            "subscription_expires_at",
            "subscription_started_at",
            "subscription_status",
            "phone",
        ]:
            if _column_exists("app_users", name):
                op.drop_column("app_users", name)
