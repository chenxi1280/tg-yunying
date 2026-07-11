"""add AI group provider fallback switches

Revision ID: 0090_ai_group_fallback
Revises: 0089_rank_deboost_runtime_index
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0090_ai_group_fallback"
down_revision = "0089_rank_deboost_runtime_index"
branch_labels = None
depends_on = None

TABLE = "tenant_ai_settings"
COLUMNS = (
    "ai_group_model_fallback_enabled",
    "ai_group_grok_fallback_enabled",
    "ai_group_static_fallback_enabled",
)


def upgrade() -> None:
    existing = _column_names()
    for name in COLUMNS:
        if name not in existing:
            op.add_column(TABLE, sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    existing = _column_names()
    for name in reversed(COLUMNS):
        if name in existing:
            op.drop_column(TABLE, name)


def _column_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(TABLE)}
