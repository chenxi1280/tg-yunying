"""add search rank deboost alert table

Revision ID: 0085_search_rank_deboost_alerts
Revises: 0084_search_rank_deboost
Create Date: 2026-07-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0085_search_rank_deboost_alerts"
down_revision = "0084_search_rank_deboost"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if _has_table("search_rank_deboost_alerts"):
        return
    op.create_table(
        "search_rank_deboost_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
        sa.Column("alert_type", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("severity", sa.String(length=20), nullable=False, server_default="warning"),
        sa.Column("task_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("action_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
        sa.Column("context", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("reason_code", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="alerting"),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("recovered_at", sa.DateTime(), nullable=True),
    )
    _create_index_if_missing(
        "ix_search_rank_deboost_alert_tenant_type_status",
        "search_rank_deboost_alerts",
        ["tenant_id", "alert_type", "status"],
    )
    _create_index_if_missing(
        "ix_search_rank_deboost_alert_task",
        "search_rank_deboost_alerts",
        ["tenant_id", "task_id"],
    )


def downgrade() -> None:
    _drop_table_if_exists("search_rank_deboost_alerts")


def _create_index_if_missing(name: str, table_name: str, columns: list[str]) -> None:
    if not _has_table(table_name):
        return
    if name in _index_names(table_name):
        return
    op.create_index(name, table_name, columns)


def _index_names(table_name: str) -> set[str]:
    if not _has_table(table_name):
        return set()
    return {row["name"] for row in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _drop_table_if_exists(table_name: str) -> None:
    if _has_table(table_name):
        op.drop_table(table_name)


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()
