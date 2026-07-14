"""index channel comment action history lookups

Revision ID: 0094_channel_comment_history
Revises: 0093_runtime_stats_indexes
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0094_channel_comment_history"
down_revision = "0093_runtime_stats_indexes"
branch_labels = None
depends_on = None

TABLE_NAME = "actions"
INDEX_NAME = "ix_actions_channel_comment_history"
LEGACY_INDEX_NAME = "ix_actions_channel_comment_legacy_history"
INDEX_KEYS = (
    (INDEX_NAME, "channel_message_id"),
    (LEGACY_INDEX_NAME, "message_id"),
)
COMMENT_HISTORY_STATUSES = "'pending', 'claiming', 'executing', 'success', 'unknown_after_send'"


def upgrade() -> None:
    _require_table()
    existing = _index_names()
    bind = op.get_bind()
    for index_name, message_key in INDEX_KEYS:
        if index_name in existing:
            continue
        if bind.dialect.name == "postgresql":
            _create_postgres_index(index_name, message_key)
        else:
            _create_sqlite_index(index_name, message_key)


def downgrade() -> None:
    _require_table()
    existing = _index_names(valid_only=False)
    for index_name, _message_key in reversed(INDEX_KEYS):
        if index_name not in existing:
            continue
        if op.get_bind().dialect.name == "postgresql":
            with op.get_context().autocommit_block():
                op.execute(f"DROP INDEX CONCURRENTLY {index_name}")
        else:
            op.drop_index(index_name, table_name=TABLE_NAME)


def _create_postgres_index(index_name: str, message_key: str) -> None:
    statement = (
        f"CREATE INDEX CONCURRENTLY {index_name} ON {TABLE_NAME} "
        "(tenant_id, ((payload ->> 'channel_target_id')::varchar), "
        f"((payload ->> '{message_key}')::varchar), created_at DESC) "
        f"WHERE action_type = 'post_comment' AND status IN ({COMMENT_HISTORY_STATUSES})"
    )
    with op.get_context().autocommit_block():
        op.execute(statement)


def _create_sqlite_index(index_name: str, message_key: str) -> None:
    op.execute(
        f"CREATE INDEX {index_name} ON {TABLE_NAME} "
        "(tenant_id, json_extract(payload, '$.channel_target_id'), "
        f"json_extract(payload, '$.{message_key}'), created_at DESC) "
        f"WHERE action_type = 'post_comment' AND status IN ({COMMENT_HISTORY_STATUSES})"
    )


def _require_table() -> None:
    if TABLE_NAME not in sa.inspect(op.get_bind()).get_table_names():
        raise RuntimeError(f"required table missing: {TABLE_NAME}")


def _index_names(*, valid_only: bool = True) -> set[str]:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        rows = bind.execute(sa.text(f"PRAGMA index_list('{TABLE_NAME}')")).mappings()
        return {row["name"] for row in rows}
    if bind.dialect.name != "postgresql":
        return {item["name"] for item in sa.inspect(bind).get_indexes(TABLE_NAME)}
    validity_filter = "AND index_meta.indisvalid" if valid_only else ""
    query = sa.text(
        "SELECT index_class.relname FROM pg_index AS index_meta "
        "JOIN pg_class AS table_class ON table_class.oid = index_meta.indrelid "
        "JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid "
        "JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace "
        "WHERE table_class.relname = :table_name AND namespace.nspname = current_schema() "
        f"{validity_filter}"
    )
    return set(bind.execute(query, {"table_name": TABLE_NAME}).scalars())
