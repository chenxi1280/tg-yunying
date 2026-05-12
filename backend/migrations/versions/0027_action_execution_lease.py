"""action execution lease

Revision ID: 0027_action_execution_lease
Revises: 0026_scheduling_runtime_policy
Create Date: 2026-05-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0027_action_execution_lease"
down_revision = "0026_scheduling_runtime_policy"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    _add_column_if_missing("actions", sa.Column("lease_owner", sa.String(length=120), nullable=False, server_default=""))
    _add_column_if_missing("actions", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for name in ["lease_expires_at", "lease_owner"]:
        if name in _columns("actions"):
            op.drop_column("actions", name)
