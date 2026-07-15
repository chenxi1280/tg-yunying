"""index production action runtime queries

Revision ID: 0096_action_runtime_indexes
Revises: 0095_retention_fk_indexes
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0096_action_runtime_indexes"
down_revision = "0095_retention_fk_indexes"
branch_labels = None
depends_on = None

INDEX_NAMES = (
    "ix_actions_ai_generation_status_counts",
    "ix_actions_ai_generation_outcome_counts",
    "ix_actions_task_type_executed_at",
)
POSTGRES_CREATE_STATEMENTS = (
    "CREATE INDEX CONCURRENTLY ix_actions_ai_generation_status_counts "
    "ON actions (tenant_id, task_id, (CAST(payload ->> 'ai_generation_status' AS VARCHAR))) "
    "WHERE action_type = 'send_message'",
    "CREATE INDEX CONCURRENTLY ix_actions_ai_generation_outcome_counts "
    "ON actions (tenant_id, task_id, (CAST(result ->> 'generation_outcome' AS VARCHAR))) "
    "WHERE action_type = 'send_message'",
    "CREATE INDEX CONCURRENTLY ix_actions_task_type_executed_at "
    "ON actions (tenant_id, task_id, action_type, executed_at)",
)
SQLITE_CREATE_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS ix_actions_ai_generation_status_counts "
    "ON actions (tenant_id, task_id, CAST(JSON_EXTRACT(payload, '$.ai_generation_status') AS VARCHAR)) "
    "WHERE action_type = 'send_message'",
    "CREATE INDEX IF NOT EXISTS ix_actions_ai_generation_outcome_counts "
    "ON actions (tenant_id, task_id, CAST(JSON_EXTRACT(result, '$.generation_outcome') AS VARCHAR)) "
    "WHERE action_type = 'send_message'",
    "CREATE INDEX IF NOT EXISTS ix_actions_task_type_executed_at "
    "ON actions (tenant_id, task_id, action_type, executed_at)",
)


def upgrade() -> None:
    _require_actions_table()
    existing = _index_names()
    statements = POSTGRES_CREATE_STATEMENTS if _is_postgres() else SQLITE_CREATE_STATEMENTS
    for name, statement in zip(INDEX_NAMES, statements, strict=True):
        if name not in existing:
            _execute_ddl(statement)


def downgrade() -> None:
    _require_actions_table()
    existing = _index_names(valid_only=False)
    for name in reversed(INDEX_NAMES):
        if name in existing:
            _execute_ddl(f"DROP INDEX {'CONCURRENTLY ' if _is_postgres() else ''}{name}")


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
    if bind.dialect.name != "postgresql":
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
