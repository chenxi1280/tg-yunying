"""commercial config users and token ledgers

Revision ID: 0014_commercial_config
Revises: 0013_continuous_campaigns
Create Date: 2026-05-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0014_commercial_config"
down_revision = "0013_continuous_campaigns"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_exists(table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(_bind()).get_columns(table)}


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if _table_exists(table) and not _column_exists(table, column.name):
        op.add_column(table, column)


def upgrade() -> None:
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
    op.create_index("ix_subscription_plans_active", "subscription_plans", ["is_active"], if_not_exists=True)

    op.execute(
        sa.text(
            """
            INSERT INTO subscription_plans (plan_type, name, duration_days, token_quota, is_active, note, created_at, updated_at)
            VALUES
              ('monthly', '月卡', 30, 500000, true, '默认月卡套餐', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
              ('yearly', '年卡', 365, 6000000, true, '默认年卡套餐', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (plan_type) DO NOTHING
            """
        )
    )

    if _table_exists("app_users"):
        _add_column_if_missing("app_users", sa.Column("token_balance", sa.Integer(), nullable=False, server_default="0"))
        _add_column_if_missing("app_users", sa.Column("token_quota_total", sa.Integer(), nullable=False, server_default="0"))
        _add_column_if_missing("app_users", sa.Column("menu_permissions", sa.Text(), nullable=False, server_default=""))

    if _table_exists("activation_codes"):
        _add_column_if_missing("activation_codes", sa.Column("plan_id", sa.Integer(), sa.ForeignKey("subscription_plans.id"), nullable=True))
        _add_column_if_missing("activation_codes", sa.Column("plan_name", sa.String(length=80), nullable=False, server_default=""))
        _add_column_if_missing("activation_codes", sa.Column("token_quota", sa.Integer(), nullable=False, server_default="0"))
        op.execute(
            sa.text(
                """
                UPDATE activation_codes AS code
                SET plan_id = plan.id,
                    plan_name = COALESCE(NULLIF(code.plan_name, ''), plan.name),
                    token_quota = CASE WHEN code.token_quota = 0 THEN plan.token_quota ELSE code.token_quota END
                FROM subscription_plans AS plan
                WHERE code.plan_type = plan.plan_type
                """
            )
        )
        op.create_index("ix_activation_codes_plan_id", "activation_codes", ["plan_id"], if_not_exists=True)

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
    op.create_index("ix_user_token_ledgers_user_created", "user_token_ledgers", ["user_id", "created_at"], if_not_exists=True)

    if _table_exists("group_archives"):
        _add_column_if_missing("group_archives", sa.Column("collection_account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True))
        _add_column_if_missing("group_archives", sa.Column("started_at", sa.DateTime(), nullable=True))
        _add_column_if_missing("group_archives", sa.Column("finished_at", sa.DateTime(), nullable=True))
        _add_column_if_missing("group_archives", sa.Column("last_synced_at", sa.DateTime(), nullable=True))
    if _table_exists("archived_messages"):
        _add_column_if_missing("archived_messages", sa.Column("sender_peer_id", sa.String(length=120), nullable=False, server_default=""))
        _add_column_if_missing("archived_messages", sa.Column("remote_message_id", sa.String(length=160), nullable=False, server_default=""))
    if _table_exists("archived_members"):
        _add_column_if_missing("archived_members", sa.Column("peer_id", sa.String(length=120), nullable=False, server_default=""))
        _add_column_if_missing("archived_members", sa.Column("last_seen_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    if _table_exists("archived_members"):
        for name in ["last_seen_at", "peer_id"]:
            if _column_exists("archived_members", name):
                op.drop_column("archived_members", name)
    if _table_exists("archived_messages"):
        for name in ["remote_message_id", "sender_peer_id"]:
            if _column_exists("archived_messages", name):
                op.drop_column("archived_messages", name)
    if _table_exists("group_archives"):
        for name in ["last_synced_at", "finished_at", "started_at", "collection_account_id"]:
            if _column_exists("group_archives", name):
                op.drop_column("group_archives", name)
    if _table_exists("user_token_ledgers"):
        op.drop_index("ix_user_token_ledgers_user_created", table_name="user_token_ledgers", if_exists=True)
        op.drop_table("user_token_ledgers")
    if _table_exists("activation_codes"):
        op.drop_index("ix_activation_codes_plan_id", table_name="activation_codes", if_exists=True)
        for name in ["token_quota", "plan_name", "plan_id"]:
            if _column_exists("activation_codes", name):
                op.drop_column("activation_codes", name)
    if _table_exists("app_users"):
        for name in ["menu_permissions", "token_quota_total", "token_balance"]:
            if _column_exists("app_users", name):
                op.drop_column("app_users", name)
    if _table_exists("subscription_plans"):
        op.drop_index("ix_subscription_plans_active", table_name="subscription_plans", if_exists=True)
        op.drop_table("subscription_plans")
