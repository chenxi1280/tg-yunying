"""operation issue context fields

Revision ID: 0045_operation_issue_context
Revises: 0044_operation_plan_models
Create Date: 2026-05-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0045_operation_issue_context"
down_revision = "0044_operation_plan_models"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _tables() -> set[str]:
    return set(sa.inspect(_bind()).get_table_names())


def _has_table(name: str) -> bool:
    return name in _tables()


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(column["name"] == column_name for column in sa.inspect(_bind()).get_columns(table_name))


def upgrade() -> None:
    if _has_table("operation_issue"):
        for column in (
            sa.Column("affected_task_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("affected_account_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("handling_mode", sa.String(length=30), nullable=False, server_default="modal"),
            sa.Column("return_to", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("claimed_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        ):
            if not _has_column("operation_issue", column.name):
                op.add_column("operation_issue", column)

    if not _has_table("operation_issue_sources"):
        op.create_table(
            "operation_issue_sources",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("issue_id", sa.String(length=36), sa.ForeignKey("operation_issue.id"), nullable=False),
            sa.Column("source_type", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("source_id", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("failure_type", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("latest_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("summary", sa.JSON(), nullable=False, server_default="{}"),
        )
        op.create_unique_constraint("uq_operation_issue_sources_source", "operation_issue_sources", ["tenant_id", "issue_id", "source_type", "source_id"])
        op.create_index("ix_operation_issue_sources_issue", "operation_issue_sources", ["tenant_id", "issue_id", "latest_seen_at"])

    if not _has_table("operation_issue_accounts"):
        op.create_table(
            "operation_issue_accounts",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("issue_id", sa.String(length=36), sa.ForeignKey("operation_issue.id"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("impact_type", sa.String(length=80), nullable=False, server_default="execution_failure"),
            sa.Column("latest_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("summary", sa.JSON(), nullable=False, server_default="{}"),
        )
        op.create_unique_constraint("uq_operation_issue_accounts_account", "operation_issue_accounts", ["tenant_id", "issue_id", "account_id", "impact_type"])
        op.create_index("ix_operation_issue_accounts_issue", "operation_issue_accounts", ["tenant_id", "issue_id", "latest_seen_at"])


def downgrade() -> None:
    for table in ("operation_issue_accounts", "operation_issue_sources"):
        if _has_table(table):
            op.drop_table(table)
    if _has_table("operation_issue"):
        for name in ("claimed_at", "claimed_by", "return_to", "handling_mode", "affected_account_count", "affected_task_count"):
            if _has_column("operation_issue", name):
                op.drop_column("operation_issue", name)
