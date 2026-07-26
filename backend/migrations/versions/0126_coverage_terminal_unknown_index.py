"""cover terminal unknown reservations in daily coverage recovery index

Revision ID: 0126_coverage_terminal_unknown
Revises: 0125_group_bot_controls
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0126_coverage_terminal_unknown"
down_revision = "0125_group_bot_controls"
branch_labels = None
depends_on = None

TABLE_NAME = "task_account_daily_coverage"
OLD_INDEX = "ix_task_daily_coverage_recovery_terminal"
INDEX_NAME = "ix_task_daily_coverage_recovery_terminal_v2"
POSTGRES_CREATE = (
    f"CREATE INDEX CONCURRENTLY {INDEX_NAME} "
    "ON task_account_daily_coverage (coverage_date, updated_at, id) "
    "INCLUDE (reserved_action_id) "
    "WHERE reserved_action_id IS NOT NULL AND state IN ('reserved', 'sending', 'unknown')"
)
SQLITE_CREATE = (
    f"CREATE INDEX {INDEX_NAME} "
    "ON task_account_daily_coverage (coverage_date, updated_at, id) "
    "WHERE reserved_action_id IS NOT NULL AND state IN ('reserved', 'sending', 'unknown')"
)
POSTGRES_OLD_CREATE = (
    f"CREATE INDEX CONCURRENTLY {OLD_INDEX} "
    "ON task_account_daily_coverage (coverage_date, updated_at, id) "
    "INCLUDE (reserved_action_id) "
    "WHERE reserved_action_id IS NOT NULL AND state IN ('reserved', 'sending')"
)
SQLITE_OLD_CREATE = (
    f"CREATE INDEX {OLD_INDEX} "
    "ON task_account_daily_coverage (coverage_date, updated_at, id) "
    "WHERE reserved_action_id IS NOT NULL AND state IN ('reserved', 'sending')"
)


def upgrade() -> None:
    _require_table()
    _create_index(INDEX_NAME, POSTGRES_CREATE, SQLITE_CREATE)
    _drop_index(OLD_INDEX)


def downgrade() -> None:
    _require_table()
    _create_index(OLD_INDEX, POSTGRES_OLD_CREATE, SQLITE_OLD_CREATE)
    _drop_index(INDEX_NAME)


def _create_index(name: str, postgres_statement: str, sqlite_statement: str) -> None:
    existing = _index_names(valid_only=False)
    valid = _index_names()
    if name in valid:
        return
    if name in existing:
        raise RuntimeError(f"invalid index exists: {name}")
    _execute_ddl(postgres_statement if _is_postgres() else sqlite_statement)


def _drop_index(name: str) -> None:
    if name not in _index_names(valid_only=False):
        return
    _execute_ddl(f"DROP INDEX {'CONCURRENTLY ' if _is_postgres() else ''}{name}")


def _execute_ddl(statement: str) -> None:
    if not _is_postgres():
        op.execute(statement)
        return
    with op.get_context().autocommit_block():
        op.execute(statement)


def _require_table() -> None:
    if TABLE_NAME not in sa.inspect(op.get_bind()).get_table_names():
        raise RuntimeError(f"required table missing: {TABLE_NAME}")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _index_names(*, valid_only: bool = True) -> set[str]:
    bind = op.get_bind()
    if not _is_postgres():
        return {item["name"] for item in sa.inspect(bind).get_indexes(TABLE_NAME)}
    validity_filter = "AND index_meta.indisvalid" if valid_only else ""
    statement = sa.text(
        "SELECT index_class.relname FROM pg_index AS index_meta "
        "JOIN pg_class AS table_class ON table_class.oid = index_meta.indrelid "
        "JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid "
        "JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace "
        "WHERE table_class.relname = :table_name AND namespace.nspname = current_schema() "
        f"{validity_filter}"
    )
    return set(bind.execute(statement, {"table_name": TABLE_NAME}).scalars())
