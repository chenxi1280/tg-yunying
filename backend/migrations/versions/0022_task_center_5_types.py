"""task center five task types

Revision ID: 0022_task_center_5_types
Revises: 0020_campaign_backoff
Create Date: 2026-05-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0022_task_center_5_types"
down_revision = "0020_campaign_backoff"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _index_names(table: str) -> set[str]:
    if not _table_exists(table):
        return set()
    return {item["name"] for item in sa.inspect(_bind()).get_indexes(table)}


def upgrade() -> None:
    if not _table_exists("tasks"):
        op.create_table(
            "tasks",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("type", sa.String(length=30), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("timezone", sa.String(length=50), nullable=False, server_default="Asia/Shanghai"),
            sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=True),
            sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column("max_duration_hours", sa.Integer(), nullable=True),
            sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("account_config", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("pacing_config", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("failure_policy", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("type_config", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("stats", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("delete_reason", sa.String(length=255), nullable=False, server_default=""),
        )
    if not _table_exists("actions"):
        op.create_table(
            "actions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id"), nullable=False),
            sa.Column("task_type", sa.String(length=30), nullable=False),
            sa.Column("action_type", sa.String(length=30), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
            sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("result", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    if not _table_exists("review_queue"):
        op.create_table(
            "review_queue",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id"), nullable=False),
            sa.Column("action_id", sa.String(length=36), sa.ForeignKey("actions.id"), nullable=False),
            sa.Column("content_preview", sa.Text(), nullable=False, server_default=""),
            sa.Column("source_info", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("reviewed_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reject_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    if not _table_exists("message_fingerprints"):
        op.create_table(
            "message_fingerprints",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("source_group_id", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("fingerprint", sa.String(length=64), nullable=False),
            sa.Column("semantic_hash", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("original_text", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    for name, table, columns in [
        ("idx_tasks_status", "tasks", ["status"]),
        ("idx_tasks_type", "tasks", ["type"]),
        ("idx_tasks_next_run", "tasks", ["status", "next_run_at"]),
        ("idx_actions_task", "actions", ["task_id", "status"]),
        ("idx_actions_scheduled", "actions", ["status", "scheduled_at"]),
        ("idx_review_status", "review_queue", ["status"]),
        ("idx_fingerprints_source", "message_fingerprints", ["source_group_id", "fingerprint"]),
        ("idx_fingerprints_time", "message_fingerprints", ["created_at"]),
    ]:
        if name not in _index_names(table):
            op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table in [
        ("idx_fingerprints_time", "message_fingerprints"),
        ("idx_fingerprints_source", "message_fingerprints"),
        ("idx_review_status", "review_queue"),
        ("idx_actions_scheduled", "actions"),
        ("idx_actions_task", "actions"),
        ("idx_tasks_next_run", "tasks"),
        ("idx_tasks_type", "tasks"),
        ("idx_tasks_status", "tasks"),
    ]:
        if name in _index_names(table):
            op.drop_index(name, table_name=table)
    for table in ["message_fingerprints", "review_queue", "actions", "tasks"]:
        if _table_exists(table):
            op.drop_table(table)
