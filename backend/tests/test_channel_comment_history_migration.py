from __future__ import annotations

from contextlib import contextmanager
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, DateTime, Integer, JSON, MetaData, String, Table, create_engine, text


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0094_channel_comment_history.py"


def test_channel_comment_history_migration_is_idempotent_and_reversible_on_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    _action_metadata().create_all(engine)
    migration = _migration_module()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        upgraded = _sqlite_index_names(connection)
        migration.downgrade()
        migration.downgrade()
        downgraded = _sqlite_index_names(connection)

    assert {name for name, _key in migration.INDEX_KEYS}.issubset(upgraded)
    assert not ({name for name, _key in migration.INDEX_KEYS} & downgraded)


def test_channel_comment_history_postgres_ddl_matches_string_cast_query_shape(monkeypatch) -> None:
    migration = _migration_module()
    events: list[str] = []
    operation = _RecordingPostgresOp(events)
    monkeypatch.setattr(migration, "op", operation)
    monkeypatch.setattr(migration, "_require_table", lambda: None)
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: set())

    migration.upgrade()

    statement = events[1]
    legacy_statement = events[4]
    assert events[0] == "enter_autocommit"
    assert events[-1] == "exit_autocommit"
    assert "CREATE INDEX CONCURRENTLY" in statement
    assert "(payload ->> 'channel_target_id')::varchar" in statement
    assert "(payload ->> 'channel_message_id')::varchar" in statement
    assert "(payload ->> 'message_id')::varchar" in legacy_statement
    assert "action_type = 'post_comment'" in statement
    assert "unknown_after_send" in statement


def test_channel_comment_history_postgres_upgrade_and_downgrade_are_idempotent(monkeypatch) -> None:
    migration = _migration_module()
    events: list[str] = []
    operation = _RecordingPostgresOp(events)
    monkeypatch.setattr(migration, "op", operation)
    monkeypatch.setattr(migration, "_require_table", lambda: None)
    index_names = {name for name, _key in migration.INDEX_KEYS}
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: index_names)

    migration.upgrade()
    assert events == []
    migration.downgrade()
    assert events == [
        "enter_autocommit",
        f"DROP INDEX CONCURRENTLY {migration.LEGACY_INDEX_NAME}",
        "exit_autocommit",
        "enter_autocommit",
        f"DROP INDEX CONCURRENTLY {migration.INDEX_NAME}",
        "exit_autocommit",
    ]


def _migration_module():
    spec = importlib.util.spec_from_file_location("channel_comment_history_0094", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _action_metadata() -> MetaData:
    metadata = MetaData()
    Table(
        "actions",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("tenant_id", Integer),
        Column("action_type", String(30)),
        Column("status", String(20)),
        Column("payload", JSON),
        Column("created_at", DateTime),
    )
    return metadata


def _sqlite_index_names(connection) -> set[str]:
    rows = connection.execute(text("PRAGMA index_list('actions')")).mappings()
    return {row["name"] for row in rows}


class _RecordingPostgresOp:
    def __init__(self, events: list[str]):
        self.events = events
        self.bind = type("Bind", (), {"dialect": type("Dialect", (), {"name": "postgresql"})()})()

    def get_bind(self):
        return self.bind

    def get_context(self):
        events = self.events

        class RecordingContext:
            @contextmanager
            def autocommit_block(self):
                events.append("enter_autocommit")
                try:
                    yield
                finally:
                    events.append("exit_autocommit")

        return RecordingContext()

    def execute(self, statement) -> None:
        self.events.append(str(statement))
