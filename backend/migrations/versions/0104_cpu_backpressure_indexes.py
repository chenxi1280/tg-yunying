"""index planner and metrics CPU backpressure hot paths

Revision ID: 0104_cpu_backpressure_indexes
Revises: 0103_group_context_recent_index
Create Date: 2026-07-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0104_cpu_backpressure_indexes"
down_revision = "0103_group_context_recent_index"
branch_labels = None
depends_on = None

INDEX_DEFINITIONS = (
    (
        "ix_actions_planner_open_normal_global",
        "actions",
        "CREATE INDEX CONCURRENTLY ix_actions_planner_open_normal_global "
        "ON actions (scheduled_at, id) "
        "WHERE status IN ('pending', 'claiming', 'executing') "
        "AND NOT (action_type = 'send_message' "
        "AND CAST(payload ->> 'hard_hourly_target' AS BOOLEAN) IS TRUE)",
        "CREATE INDEX IF NOT EXISTS ix_actions_planner_open_normal_global "
        "ON actions (scheduled_at, id) "
        "WHERE status IN ('pending', 'claiming', 'executing') "
        "AND NOT (action_type = 'send_message' "
        "AND JSON_EXTRACT(payload, '$.hard_hourly_target') IS 1)",
    ),
    (
        "ix_actions_planner_open_normal_task",
        "actions",
        "CREATE INDEX CONCURRENTLY ix_actions_planner_open_normal_task "
        "ON actions (task_id, scheduled_at, id) "
        "WHERE status IN ('pending', 'claiming', 'executing') "
        "AND NOT (action_type = 'send_message' "
        "AND CAST(payload ->> 'hard_hourly_target' AS BOOLEAN) IS TRUE)",
        "CREATE INDEX IF NOT EXISTS ix_actions_planner_open_normal_task "
        "ON actions (task_id, scheduled_at, id) "
        "WHERE status IN ('pending', 'claiming', 'executing') "
        "AND NOT (action_type = 'send_message' "
        "AND JSON_EXTRACT(payload, '$.hard_hourly_target') IS 1)",
    ),
    (
        "ix_actions_planner_open_hard_hourly_task",
        "actions",
        "CREATE INDEX CONCURRENTLY ix_actions_planner_open_hard_hourly_task "
        "ON actions (task_id, scheduled_at, id) "
        "WHERE status IN ('pending', 'claiming', 'executing') "
        "AND action_type = 'send_message' "
        "AND CAST(payload ->> 'hard_hourly_target' AS BOOLEAN) IS TRUE",
        "CREATE INDEX IF NOT EXISTS ix_actions_planner_open_hard_hourly_task "
        "ON actions (task_id, scheduled_at, id) "
        "WHERE status IN ('pending', 'claiming', 'executing') "
        "AND action_type = 'send_message' "
        "AND JSON_EXTRACT(payload, '$.hard_hourly_target') IS 1",
    ),
    (
        "ix_worker_heartbeats_last_seen_at",
        "worker_heartbeats",
        "CREATE INDEX CONCURRENTLY ix_worker_heartbeats_last_seen_at "
        "ON worker_heartbeats (last_seen_at)",
        "CREATE INDEX IF NOT EXISTS ix_worker_heartbeats_last_seen_at "
        "ON worker_heartbeats (last_seen_at)",
    ),
    (
        "ix_runtime_metric_snapshots_metric_dimension_captured",
        "runtime_metric_snapshots",
        "CREATE INDEX CONCURRENTLY ix_runtime_metric_snapshots_metric_dimension_captured "
        "ON runtime_metric_snapshots (metric_name, dimension_type, dimension_id, captured_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_runtime_metric_snapshots_metric_dimension_captured "
        "ON runtime_metric_snapshots (metric_name, dimension_type, dimension_id, captured_at DESC)",
    ),
)
REQUIRED_TABLES = {table_name for _index_name, table_name, _postgres, _sqlite in INDEX_DEFINITIONS}


def upgrade() -> None:
    _require_tables()
    for index_name, table_name, postgres_sql, sqlite_sql in INDEX_DEFINITIONS:
        if index_name not in _index_names(table_name):
            _execute_ddl(postgres_sql if _is_postgres() else sqlite_sql)


def downgrade() -> None:
    _require_tables()
    for index_name, table_name, _postgres_sql, _sqlite_sql in reversed(INDEX_DEFINITIONS):
        if index_name in _index_names(table_name, valid_only=False):
            concurrent = "CONCURRENTLY " if _is_postgres() else ""
            _execute_ddl(f"DROP INDEX {concurrent}{index_name}")


def _execute_ddl(statement: str) -> None:
    if not _is_postgres():
        op.execute(statement)
        return
    with op.get_context().autocommit_block():
        op.execute(statement)


def _require_tables() -> None:
    table_names = set(sa.inspect(op.get_bind()).get_table_names())
    missing = REQUIRED_TABLES - table_names
    if missing:
        raise RuntimeError(f"required tables missing: {', '.join(sorted(missing))}")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _index_names(table_name: str, *, valid_only: bool = True) -> set[str]:
    bind = op.get_bind()
    if not _is_postgres():
        query = sa.text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :table_name")
        return set(bind.execute(query, {"table_name": table_name}).scalars())
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
