"""index recent task action cycle scans

Revision ID: 0097_action_cycle_scan_index
Revises: 0096_action_runtime_indexes
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0097_action_cycle_scan_index"
down_revision = "0096_action_runtime_indexes"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_actions_task_type_created_at"
POSTGRES_CREATE = (
    f"CREATE INDEX CONCURRENTLY {INDEX_NAME} "
    "ON actions (tenant_id, task_id, action_type, created_at DESC)"
)


def upgrade() -> None:
    _require_actions_table()
    if INDEX_NAME in _index_names():
        return
    if _is_postgres():
        with op.get_context().autocommit_block():
            op.execute(POSTGRES_CREATE)
        return
    op.create_index(INDEX_NAME, "actions", ["tenant_id", "task_id", "action_type", "created_at"])


def downgrade() -> None:
    _require_actions_table()
    if INDEX_NAME not in _index_names(valid_only=False):
        return
    if _is_postgres():
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY {INDEX_NAME}")
        return
    op.drop_index(INDEX_NAME, table_name="actions")


def _require_actions_table() -> None:
    if "actions" not in sa.inspect(op.get_bind()).get_table_names():
        raise RuntimeError("required table missing: actions")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _index_names(*, valid_only: bool = True) -> set[str]:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return {item["name"] for item in sa.inspect(bind).get_indexes("actions")}
    validity_filter = "AND index_meta.indisvalid" if valid_only else ""
    query = sa.text(
        "SELECT index_class.relname FROM pg_index AS index_meta "
        "JOIN pg_class AS table_class ON table_class.oid = index_meta.indrelid "
        "JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid "
        "JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace "
        "WHERE table_class.relname = 'actions' AND namespace.nspname = current_schema() "
        f"{validity_filter}"
    )
    return set(bind.execute(query).scalars())
