"""task action detail paging index

Revision ID: 0063_task_action_paging
Revises: 0062_group_rescue
Create Date: 2026-06-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0063_task_action_paging"
down_revision = "0062_group_rescue"
branch_labels = None
depends_on = None


INDEXES = {
    "ix_actions_task_schedule_page": ["tenant_id", "task_id", "scheduled_at", "created_at"],
    "ix_actions_task_status_schedule_page": ["tenant_id", "task_id", "status", "scheduled_at", "created_at"],
    "ix_actions_task_type_schedule_page": ["tenant_id", "task_id", "action_type", "scheduled_at", "created_at"],
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
