"""index metrics voice-anchor summary count

Revision ID: 0111_metrics_summary_anchor
Revises: 0110_hard_hourly_recovery_cpu
Create Date: 2026-07-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0111_metrics_summary_anchor"
down_revision = "0110_hard_hourly_recovery_cpu"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_actions_task_voice_anchor_fact"
POSTGRES_CREATE = (
    f"CREATE INDEX CONCURRENTLY {INDEX_NAME} ON actions (tenant_id, task_id) "
    "WHERE action_type = 'send_message' "
    "AND CAST(result ->> 'voice_profile_anchor_rewritten' AS BOOLEAN) IS TRUE"
)
SQLITE_CREATE = (
    f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON actions (tenant_id, task_id) "
    "WHERE action_type = 'send_message' "
    "AND JSON_EXTRACT(result, '$.voice_profile_anchor_rewritten') IS 1"
)


def upgrade() -> None:
    _require_actions_table()
    if INDEX_NAME not in _index_names():
        _execute_ddl(POSTGRES_CREATE if _is_postgres() else SQLITE_CREATE)


def downgrade() -> None:
    _require_actions_table()
    if INDEX_NAME in _index_names(valid_only=False):
        _execute_ddl(f"DROP INDEX {'CONCURRENTLY ' if _is_postgres() else ''}{INDEX_NAME}")


def _execute_ddl(statement: str) -> None:
    if not _is_postgres():
        op.execute(statement)
        return
    with op.get_context().autocommit_block():
        op.execute(statement)


def _require_actions_table() -> None:
    if "actions" not in sa.inspect(op.get_bind()).get_table_names():
        raise RuntimeError("required table missing: actions")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _index_names(*, valid_only: bool = True) -> set[str]:
    bind = op.get_bind()
    if not _is_postgres():
        query = sa.text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'actions'")
        return set(bind.execute(query).scalars())
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
