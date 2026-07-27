"""bind AI message memory to account mask evidence

Revision ID: 0129_ai_memory_account_mask
Revises: 0128_ai_group_daily_targets
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0129_ai_memory_account_mask"
down_revision = "0128_ai_group_daily_targets"
branch_labels = None
depends_on = None

TABLE = "ai_group_message_memory"
INDEX = "ix_ai_group_message_memory_account_window"


def upgrade() -> None:
    for column in _mask_columns():
        if not _has_column(TABLE, column.name):
            op.add_column(TABLE, column)
    if not _has_index(TABLE, INDEX):
        op.create_index(INDEX, TABLE, ["tenant_id", "account_id", "status", "planned_at"])


def downgrade() -> None:
    if _has_index(TABLE, INDEX):
        op.drop_index(INDEX, table_name=TABLE)
    for column in reversed(_mask_columns()):
        if _has_column(TABLE, column.name):
            op.drop_column(TABLE, column.name)


def _mask_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("account_mask_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("account_mask_version", sa.Integer(), nullable=True),
        sa.Column("mask_contract_version", sa.String(40), nullable=False, server_default=""),
        sa.Column("mask_snapshot_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("mask_status", sa.String(30), nullable=False, server_default=""),
        sa.Column("content_source", sa.String(40), nullable=False, server_default=""),
    )


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))
