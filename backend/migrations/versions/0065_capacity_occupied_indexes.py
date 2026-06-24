"""capacity occupied expression indexes

Revision ID: 0065_capacity_occupied_idx
Revises: 0064_metrics_count_idx
Create Date: 2026-06-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0065_capacity_occupied_idx"
down_revision = "0064_metrics_count_idx"
branch_labels = None
depends_on = None


INDEX_DEFINITIONS = {
    "ix_actions_account_occupied_at": """
        CREATE INDEX IF NOT EXISTS ix_actions_account_occupied_at
        ON actions (
            tenant_id,
            account_id,
            status,
            (coalesce(executed_at, scheduled_at))
        )
    """,
    "ix_message_tasks_account_occupied_at": """
        CREATE INDEX IF NOT EXISTS ix_message_tasks_account_occupied_at
        ON message_tasks (
            tenant_id,
            (coalesce(account_id, preferred_account_id)),
            status,
            (coalesce(sent_at, scheduled_at))
        )
    """,
}


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(table_name):
        return False
    return any(index["name"] == index_name for index in sa.inspect(bind).get_indexes(table_name))


def upgrade() -> None:
    for name, statement in INDEX_DEFINITIONS.items():
        table_name = "message_tasks" if name.startswith("ix_message_tasks") else "actions"
        if not _has_index(table_name, name):
            op.execute(sa.text(statement))


def downgrade() -> None:
    for name in reversed(INDEX_DEFINITIONS):
        table_name = "message_tasks" if name.startswith("ix_message_tasks") else "actions"
        if _has_index(table_name, name):
            op.drop_index(name, table_name=table_name)
