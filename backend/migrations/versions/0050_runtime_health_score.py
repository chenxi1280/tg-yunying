"""account runtime health score fields

Revision ID: 0050_runtime_health_score
Revises: 0049_material_cache_account
Create Date: 2026-05-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0050_runtime_health_score"
down_revision = "0049_material_cache_account"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return name in sa.inspect(_bind()).get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(column["name"] == column_name for column in sa.inspect(_bind()).get_columns(table_name))


def upgrade() -> None:
    if not _table_exists("account_runtime_summary"):
        return
    if not _column_exists("account_runtime_summary", "health_score"):
        op.add_column("account_runtime_summary", sa.Column("health_score", sa.Float(), nullable=False, server_default="100"))
    if not _column_exists("account_runtime_summary", "risk_level"):
        op.add_column("account_runtime_summary", sa.Column("risk_level", sa.String(length=20), nullable=False, server_default="A"))
    if not _column_exists("account_runtime_summary", "score_reasons"):
        op.add_column("account_runtime_summary", sa.Column("score_reasons", sa.JSON(), nullable=False, server_default="[]"))
    if not _column_exists("account_runtime_summary", "non_score_reasons"):
        op.add_column("account_runtime_summary", sa.Column("non_score_reasons", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    if not _table_exists("account_runtime_summary"):
        return
    for column_name in ("non_score_reasons", "score_reasons", "risk_level", "health_score"):
        if _column_exists("account_runtime_summary", column_name):
            op.drop_column("account_runtime_summary", column_name)
