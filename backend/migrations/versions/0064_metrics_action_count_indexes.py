"""metrics action count indexes

Revision ID: 0064_metrics_count_idx
Revises: 0063_task_action_paging
Create Date: 2026-06-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0064_metrics_count_idx"
down_revision = "0063_task_action_paging"
branch_labels = None
depends_on = None


INDEXES = {
    "ix_actions_executed_at_status": ["executed_at", "status"],
    "ix_actions_created_at": ["created_at"],
}


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(table_name):
        return False
    return any(index["name"] == index_name for index in sa.inspect(bind).get_indexes(table_name))


def upgrade() -> None:
    for name, columns in INDEXES.items():
        if not _has_index("actions", name):
            op.create_index(name, "actions", columns)


def downgrade() -> None:
    for name in reversed(INDEXES):
        if _has_index("actions", name):
            op.drop_index(name, table_name="actions")
