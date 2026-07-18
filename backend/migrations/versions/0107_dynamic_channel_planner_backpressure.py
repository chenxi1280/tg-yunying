"""repair dynamic channel planner schedule and coverage recovery scan

Revision ID: 0107_dynamic_channel_planner
Revises: 0106_hard_hourly_backpressure
Create Date: 2026-07-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0107_dynamic_channel_planner"
down_revision = "0106_hard_hourly_backpressure"
branch_labels = None
depends_on = None

TASK_TABLE = "tasks"
COVERAGE_TABLE = "task_account_daily_coverage"
RECOVERY_INDEX = "ix_task_daily_coverage_recovery_terminal"


def upgrade() -> None:
    _require_tables()
    _repair_dynamic_channel_schedule()
    _create_recovery_index()


def downgrade() -> None:
    _require_tables()
    _drop_recovery_index()


def _repair_dynamic_channel_schedule() -> None:
    op.execute(_POSTGRES_REPAIR_SQL if _is_postgres() else _SQLITE_REPAIR_SQL)


def _create_recovery_index() -> None:
    if RECOVERY_INDEX in _index_names(COVERAGE_TABLE):
        return
    statement = _POSTGRES_INDEX_SQL if _is_postgres() else _SQLITE_INDEX_SQL
    _execute_index_ddl(statement)


def _drop_recovery_index() -> None:
    if RECOVERY_INDEX not in _index_names(COVERAGE_TABLE, valid_only=False):
        return
    _execute_index_ddl(f"DROP INDEX {'CONCURRENTLY ' if _is_postgres() else ''}{RECOVERY_INDEX}")


def _execute_index_ddl(statement: str) -> None:
    if not _is_postgres():
        op.execute(statement)
        return
    with op.get_context().autocommit_block():
        op.execute(statement)


def _require_tables() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    missing = {TASK_TABLE, COVERAGE_TABLE} - tables
    if missing:
        raise RuntimeError(f"required table missing: {', '.join(sorted(missing))}")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _index_names(table: str, *, valid_only: bool = True) -> set[str]:
    bind = op.get_bind()
    if not _is_postgres():
        return {item["name"] for item in sa.inspect(bind).get_indexes(table)}
    validity_filter = "AND index_meta.indisvalid" if valid_only else ""
    statement = sa.text(
        "SELECT index_class.relname FROM pg_index AS index_meta "
        "JOIN pg_class AS table_class ON table_class.oid = index_meta.indrelid "
        "JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid "
        "JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace "
        "WHERE table_class.relname = :table_name AND namespace.nspname = current_schema() "
        f"{validity_filter}"
    )
    return set(bind.execute(statement, {"table_name": table}).scalars())


_POSTGRES_REPAIR_SQL = """
UPDATE tasks
SET next_run_at = now() + make_interval(
        secs => GREATEST(
            1,
            CASE
                WHEN COALESCE(type_config ->> 'listener_interval_seconds', '') ~ '^-?[0-9]+$'
                    THEN COALESCE(NULLIF((type_config ->> 'listener_interval_seconds')::integer, 0), 30)
                ELSE 30
            END
        )
    ),
    updated_at = now()
WHERE status = 'running'
  AND type IN ('channel_view', 'channel_like', 'channel_comment')
  AND COALESCE(type_config ->> 'message_scope', 'latest_n') = 'dynamic_new'
  AND next_run_at <= now()
"""

_SQLITE_REPAIR_SQL = """
UPDATE tasks
SET next_run_at = datetime(
        'now',
        '+8 hours',
        '+' || CASE
            WHEN CAST(COALESCE(json_extract(type_config, '$.listener_interval_seconds'), 0) AS INTEGER) > 0
                THEN CAST(json_extract(type_config, '$.listener_interval_seconds') AS INTEGER)
            ELSE 30
        END || ' seconds'
    ),
    updated_at = datetime('now')
WHERE status = 'running'
  AND type IN ('channel_view', 'channel_like', 'channel_comment')
  AND COALESCE(json_extract(type_config, '$.message_scope'), 'latest_n') = 'dynamic_new'
  AND next_run_at <= datetime('now', '+8 hours')
"""

_POSTGRES_INDEX_SQL = """
CREATE INDEX CONCURRENTLY ix_task_daily_coverage_recovery_terminal
ON task_account_daily_coverage (coverage_date, updated_at, id)
INCLUDE (reserved_action_id)
WHERE reserved_action_id IS NOT NULL AND state IN ('reserved', 'sending')
"""

_SQLITE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_task_daily_coverage_recovery_terminal
ON task_account_daily_coverage (coverage_date, updated_at, id)
WHERE reserved_action_id IS NOT NULL AND state IN ('reserved', 'sending')
"""
