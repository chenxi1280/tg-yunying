"""runtime summary read models

Revision ID: 0043_runtime_summary_models
Revises: 0042_phone_contacts_archives
Create Date: 2026-05-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0043_runtime_summary_models"
down_revision = "0042_phone_contacts_archives"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _tables() -> set[str]:
    return set(sa.inspect(_bind()).get_table_names())


def _has_table(name: str) -> bool:
    return name in _tables()


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(index["name"] == index_name for index in sa.inspect(_bind()).get_indexes(table_name))


def upgrade() -> None:
    if not _has_table("target_runtime_summary"):
        op.create_table(
            "target_runtime_summary",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="healthy"),
            sa.Column("open_issue_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed_action_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("affected_task_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("latest_failure_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("summary", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_unique_constraint("uq_target_runtime_summary_target", "target_runtime_summary", ["tenant_id", "target_id"])
        op.create_index("ix_target_runtime_summary_status", "target_runtime_summary", ["tenant_id", "target_id", "status"])

    if not _has_table("task_runtime_summary"):
        op.create_table(
            "task_runtime_summary",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id"), nullable=False),
            sa.Column("task_status", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=True),
            sa.Column("planned_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("pending_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("oldest_pending_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("latest_failure_type", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("summary", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_unique_constraint("uq_task_runtime_summary_task", "task_runtime_summary", ["tenant_id", "task_id"])
        op.create_index("ix_task_runtime_summary_task", "task_runtime_summary", ["tenant_id", "task_id"])

    if not _has_table("account_runtime_summary"):
        op.create_table(
            "account_runtime_summary",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("send_available", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("listen_available", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("join_available", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("comment_available", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("profile_available", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("code_read_available", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("remaining_capacity", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("unavailable_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("failure_trend", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_unique_constraint("uq_account_runtime_summary_account", "account_runtime_summary", ["tenant_id", "account_id"])
        op.create_index("ix_account_runtime_summary_account", "account_runtime_summary", ["tenant_id", "account_id"])

    if not _has_table("operation_issue"):
        op.create_table(
            "operation_issue",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=True),
            sa.Column("issue_type", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("severity", sa.String(length=40), nullable=False, server_default="warning"),
            sa.Column("source_task_id", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("representative_action_id", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("affected_account_ids", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("failure_type", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("failure_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("suggested_action", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="open"),
            sa.Column("summary", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_operation_issue_target_status", "operation_issue", ["tenant_id", "target_id", "status"])
        op.create_index("ix_operation_issue_type_status", "operation_issue", ["tenant_id", "issue_type", "failure_type", "status"])

    if not _has_index("actions", "ix_actions_task_status"):
        op.create_index("ix_actions_task_status", "actions", ["task_id", "status", "executed_at"])


def downgrade() -> None:
    if _has_index("actions", "ix_actions_task_status"):
        op.drop_index("ix_actions_task_status", table_name="actions")
    for name in ("operation_issue", "account_runtime_summary", "task_runtime_summary", "target_runtime_summary"):
        if _has_table(name):
            op.drop_table(name)
