"""group context sender identity

Revision ID: 0039_sender_identity
Revises: 0038_runtime_metric_snapshots
Create Date: 2026-05-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0039_sender_identity"
down_revision = "0038_runtime_metric_snapshots"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(_bind())
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    _add_column_if_missing("group_context_messages", sa.Column("sender_username", sa.String(length=120), nullable=False, server_default=""))
    _add_column_if_missing("group_context_messages", sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.false()))
    _add_column_if_missing("group_context_messages", sa.Column("sender_role", sa.String(length=40), nullable=False, server_default="member"))


def downgrade() -> None:
    columns = _columns("group_context_messages")
    for name in ("sender_role", "is_bot", "sender_username"):
        if name in columns:
            op.drop_column("group_context_messages", name)
