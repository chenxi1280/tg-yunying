"""operations center targets and tasks

Revision ID: 0017_operations_center
Revises: 0016_tg_account_soft_delete
Create Date: 2026-05-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0017_operations_center"
down_revision = "0016_tg_account_soft_delete"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def upgrade() -> None:
    if not _table_exists("operation_targets"):
        op.create_table(
            "operation_targets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("target_type", sa.String(length=20), nullable=False, server_default="group"),
            sa.Column("tg_peer_id", sa.String(length=120), nullable=False),
            sa.Column("title", sa.String(length=180), nullable=False),
            sa.Column("username", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("member_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("can_send", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("auth_status", sa.String(length=30), nullable=False, server_default="未确认"),
            sa.Column("last_sync_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "tg_peer_id", name="uq_operation_targets_tenant_peer"),
        )
    if not _table_exists("channel_messages"):
        op.create_table(
            "channel_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("channel_target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=False),
            sa.Column("message_id", sa.Integer(), nullable=False),
            sa.Column("message_url", sa.String(length=300), nullable=False, server_default=""),
            sa.Column("content_preview", sa.Text(), nullable=False, server_default=""),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "channel_target_id", "message_id", name="uq_channel_messages_tenant_channel_msg"),
        )
    if not _table_exists("operation_tasks"):
        op.create_table(
            "operation_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("task_type", sa.String(length=40), nullable=False),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=True),
            sa.Column("channel_message_id", sa.Integer(), sa.ForeignKey("channel_messages.id"), nullable=True),
            sa.Column("title", sa.String(length=180), nullable=False, server_default=""),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("reaction", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("account_ids", sa.Text(), nullable=False, server_default=""),
            sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="排队中"),
            sa.Column("failure_type", sa.String(length=60), nullable=False, server_default=""),
            sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("scheduled_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("executed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    if not _table_exists("operation_task_attempts"):
        op.create_table(
            "operation_task_attempts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("task_id", sa.Integer(), sa.ForeignKey("operation_tasks.id"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
            sa.Column("action_type", sa.String(length=40), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("failure_type", sa.String(length=60), nullable=False, server_default=""),
            sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("remote_message_id", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("executed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    if not _table_exists("manual_operation_records"):
        op.create_table(
            "manual_operation_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=True),
            sa.Column("operation_type", sa.String(length=40), nullable=False, server_default="MESSAGE_SEND"),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("failure_type", sa.String(length=60), nullable=False, server_default=""),
            sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("remote_message_id", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("actor", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )

    if _table_exists("tg_groups"):
        bind = _bind()
        bind.execute(
            sa.text(
                """
                INSERT INTO operation_targets
                    (tenant_id, target_type, tg_peer_id, title, member_count, can_send, auth_status, last_sync_at, created_at, updated_at)
                SELECT tenant_id,
                    CASE WHEN group_type = 'channel' THEN 'channel' ELSE 'group' END,
                    tg_peer_id, title, member_count, can_send, auth_status, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                FROM tg_groups
                ON CONFLICT (tenant_id, tg_peer_id) DO NOTHING
                """
            )
        )


def downgrade() -> None:
    for table in (
        "manual_operation_records",
        "operation_task_attempts",
        "operation_tasks",
        "channel_messages",
        "operation_targets",
    ):
        if _table_exists(table):
            op.drop_table(table)
