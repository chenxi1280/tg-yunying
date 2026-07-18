"""index runtime retention action scans

Revision ID: 0108_runtime_retention_idx
Revises: 0107_dynamic_channel_planner
Create Date: 2026-07-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0108_runtime_retention_idx"
down_revision = "0107_dynamic_channel_planner"
branch_labels = None
depends_on = None

ACTION_TABLE = "actions"
AUDIT_TABLE = "runtime_cleanup_audits"
ACTION_INDEX = "ix_actions_runtime_detail_retention"
AUDIT_INDEX = "ix_runtime_cleanup_audits_kind_created_at"

POSTGRES_ACTION_INDEX_SQL = (
    f"CREATE INDEX CONCURRENTLY {ACTION_INDEX} ON {ACTION_TABLE} "
    "((COALESCE(executed_at, scheduled_at, created_at)), created_at, id)"
)
SQLITE_ACTION_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS {ACTION_INDEX} ON {ACTION_TABLE} "
    "(COALESCE(executed_at, scheduled_at, created_at), created_at, id)"
)
POSTGRES_AUDIT_INDEX_SQL = (
    f"CREATE INDEX CONCURRENTLY {AUDIT_INDEX} ON {AUDIT_TABLE} "
    "((CAST(summary ->> 'cleanup_kind' AS varchar)), created_at DESC)"
)
SQLITE_AUDIT_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS {AUDIT_INDEX} ON {AUDIT_TABLE} "
    "(json_extract(summary, '$.cleanup_kind'), created_at DESC)"
)
INDEXES = (
    (ACTION_TABLE, ACTION_INDEX, POSTGRES_ACTION_INDEX_SQL, SQLITE_ACTION_INDEX_SQL),
    (AUDIT_TABLE, AUDIT_INDEX, POSTGRES_AUDIT_INDEX_SQL, SQLITE_AUDIT_INDEX_SQL),
)


def upgrade() -> None:
    _require_tables()
    _create_indexes()


def downgrade() -> None:
    _require_tables()
    _drop_indexes()


def _create_indexes() -> None:
    for table_name, index_name, postgres_statement, sqlite_statement in INDEXES:
        if index_name not in _index_names(table_name):
            _execute_ddl(postgres_statement if _is_postgres() else sqlite_statement)


def _drop_indexes() -> None:
    for table_name, index_name, _, _ in reversed(INDEXES):
        if index_name in _index_names(table_name, valid_only=False):
            _execute_ddl(f"DROP INDEX {'CONCURRENTLY ' if _is_postgres() else ''}{index_name}")


def _execute_ddl(statement: str) -> None:
    if not _is_postgres():
        op.execute(statement)
        return
    with op.get_context().autocommit_block():
        op.execute(statement)


def _require_tables() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    missing = sorted({ACTION_TABLE, AUDIT_TABLE} - tables)
    if missing:
        raise RuntimeError(f"required tables missing: {', '.join(missing)}")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _index_names(table_name: str, *, valid_only: bool = True) -> set[str]:
    bind = op.get_bind()
    if not _is_postgres():
        return {row[1] for row in bind.execute(sa.text(f"PRAGMA index_list({table_name})"))}
    validity_filter = "AND index_meta.indisvalid" if valid_only else ""
    query = sa.text(
        "SELECT index_class.relname FROM pg_index AS index_meta "
        "JOIN pg_class AS table_class ON table_class.oid = index_meta.indrelid "
        "JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid "
        "JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace "
        "WHERE table_class.relname = :table_name AND namespace.nspname = current_schema() "
        f"{validity_filter}"
    )
    return set(bind.execute(query, {"table_name": table_name}).scalars())
