from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0092_ai_message_memory_action_id_index.py"
INDEX_NAME = "ix_ai_group_message_memory_action_id"


def _migration_module():
    spec = importlib.util.spec_from_file_location("ai_message_memory_action_id_index_0092", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("action id index migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FailingPostgresOp:
    def __init__(self, events: list[str], failure: Exception):
        self.events = events
        self.failure = failure
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

    def execute(self, statement):
        self.events.append(str(statement))
        raise self.failure


def test_action_id_index_invalid_same_name_fails_without_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration_module()
    failure = RuntimeError("relation already exists with invalid index")
    events: list[str] = []
    valid_only_calls: list[bool] = []

    def index_names(*, valid_only: bool = True) -> set[str]:
        valid_only_calls.append(valid_only)
        return set() if valid_only else {INDEX_NAME}

    monkeypatch.setattr(migration, "op", _FailingPostgresOp(events, failure))
    monkeypatch.setattr(migration, "_require_table", lambda: None)
    monkeypatch.setattr(migration, "_index_names", index_names)

    with pytest.raises(RuntimeError) as exc_info:
        migration.upgrade()

    assert exc_info.value is failure
    assert valid_only_calls == [True]
    assert f"CREATE INDEX CONCURRENTLY {INDEX_NAME}" in events[1]
    assert not any("DROP INDEX" in event for event in events)
