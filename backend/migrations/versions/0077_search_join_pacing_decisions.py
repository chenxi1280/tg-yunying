"""add search join pacing decisions

Revision ID: 0077_search_join_pacing
Revises: 0076_search_join_environment
Create Date: 2026-07-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0077_search_join_pacing"
down_revision = "0076_search_join_environment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _has_table("search_join_pacing_decisions"):
        op.create_table(
            "search_join_pacing_decisions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("task_id", sa.String(length=36), nullable=False),
            sa.Column("decision_scope", sa.String(length=24), nullable=False, server_default=""),
            sa.Column("scope_key", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("tenant_timezone", sa.String(length=50), nullable=False, server_default="Asia/Shanghai"),
            sa.Column("local_date", sa.Date(), nullable=True),
            sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
            sa.Column("account_id", sa.Integer(), nullable=True),
            sa.Column("keyword_hash", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("sampled_value", sa.Float(), nullable=True),
            sa.Column("threshold", sa.Float(), nullable=True),
            sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reason", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("decision_value", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "task_id", "decision_scope", "scope_key", name="uq_search_join_pacing_decision_scope"),
        )
    _create_index_if_missing("ix_search_join_pacing_decision_task", "search_join_pacing_decisions", ["tenant_id", "task_id", "local_date"])


def downgrade() -> None:
    _drop_index_if_exists("ix_search_join_pacing_decision_task", "search_join_pacing_decisions")
    if _has_table("search_join_pacing_decisions"):
        op.drop_table("search_join_pacing_decisions")


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _index_names(table_name: str) -> set[str]:
    if not _has_table(table_name):
        return set()
    return {row["name"] for row in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _create_index_if_missing(name: str, table_name: str, columns: list[str]) -> None:
    if name not in _index_names(table_name):
        op.create_index(name, table_name, columns)


def _drop_index_if_exists(name: str, table_name: str) -> None:
    if name in _index_names(table_name):
        op.drop_index(name, table_name=table_name)
