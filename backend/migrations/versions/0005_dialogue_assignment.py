"""dialogue script account assignment

Revision ID: 0005_dialogue_assignment
Revises: 0004_account_detail_tasks
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0005_dialogue_assignment"
down_revision = "0004_account_detail_tasks"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _columns(table_name: str) -> set[str]:
    if not _table_exists(table_name):
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    _add_column_if_missing("ai_drafts", sa.Column("suggested_account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True))
    _add_column_if_missing("ai_drafts", sa.Column("sequence_index", sa.Integer(), nullable=False, server_default="0"))
    _add_column_if_missing("ai_drafts", sa.Column("reply_to_draft_id", sa.Integer(), sa.ForeignKey("ai_drafts.id"), nullable=True))
    _add_column_if_missing("message_tasks", sa.Column("preferred_account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True))


def downgrade() -> None:
    for table_name, column_names in {
        "message_tasks": ["preferred_account_id"],
        "ai_drafts": ["reply_to_draft_id", "sequence_index", "suggested_account_id"],
    }.items():
        existing = _columns(table_name)
        for column_name in column_names:
            if column_name in existing:
                op.drop_column(table_name, column_name)
