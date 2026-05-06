"""account pools clone plans and verification tasks

Revision ID: 0008_account_ops
Revises: 0007_operational
Create Date: 2026-05-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_account_ops"
down_revision = "0007_operational"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_exists(table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(_bind()).get_columns(table)}


def upgrade() -> None:
    if not _table_exists("account_pools"):
        op.create_table(
            "account_pools",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("tenant_id", "name"),
        )
    if _table_exists("tg_accounts") and not _column_exists("tg_accounts", "pool_id"):
        op.add_column("tg_accounts", sa.Column("pool_id", sa.Integer(), sa.ForeignKey("account_pools.id"), nullable=True))

    if not _table_exists("account_clone_plans"):
        op.create_table(
            "account_clone_plans",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("source_account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("target_account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("clone_scope", sa.String(length=160), nullable=False, server_default="contacts,groups"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="待确认"),
            sa.Column("items_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("items_done", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("items_failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        )
    if not _table_exists("account_clone_items"):
        op.create_table(
            "account_clone_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("plan_id", sa.Integer(), sa.ForeignKey("account_clone_plans.id"), nullable=False),
            sa.Column("source_account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("target_account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("target_type", sa.String(length=40), nullable=False),
            sa.Column("target_peer_id", sa.String(length=120), nullable=False),
            sa.Column("target_display", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="待确认"),
            sa.Column("failure_type", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("executed_at", sa.DateTime(), nullable=True),
        )
    if not _table_exists("verification_tasks"):
        op.create_table(
            "verification_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
            sa.Column("group_id", sa.Integer(), sa.ForeignKey("tg_groups.id"), nullable=True),
            sa.Column("message_task_id", sa.Integer(), sa.ForeignKey("message_tasks.id"), nullable=True),
            sa.Column("verification_type", sa.String(length=60), nullable=False, server_default="未知验证"),
            sa.Column("detected_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("suggested_action", sa.String(length=120), nullable=False, server_default="人工处理"),
            sa.Column("target_peer_id", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("target_display", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("requires_user_confirm", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="待处理"),
            sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("handled_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    if _table_exists("verification_tasks"):
        op.drop_table("verification_tasks")
    if _table_exists("account_clone_items"):
        op.drop_table("account_clone_items")
    if _table_exists("account_clone_plans"):
        op.drop_table("account_clone_plans")
    if _table_exists("tg_accounts") and _column_exists("tg_accounts", "pool_id"):
        op.drop_column("tg_accounts", "pool_id")
    if _table_exists("account_pools"):
        op.drop_table("account_pools")
