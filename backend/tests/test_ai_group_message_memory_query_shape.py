from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from datetime import timedelta
from inspect import Parameter, signature
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, event, inspect
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, AiGroupMessageMemory
from app.services._common import _now
from app.services.task_center.ai_message_memory import (
    _historical_group_ai_actions,
    _window_memories,
)


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0091_ai_message_memory_dedup_index.py"
INDEX_NAME = "ix_ai_group_message_memory_tenant_status_planned"
ACTION_INDEX_MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0092_ai_message_memory_action_id_index.py"
ACTION_INDEX_NAME = "ix_ai_group_message_memory_action_id"
POISONED_ACTION_COUNT = 100


def test_window_memories_accepts_only_session_positionally() -> None:
    positional_parameters = [
        parameter.name
        for parameter in signature(_window_memories).parameters.values()
        if parameter.kind in {Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD}
    ]

    assert positional_parameters == ["session"]


def _migration_module():
    assert MIGRATION_PATH.exists(), f"missing migration: {MIGRATION_PATH.name}"
    spec = importlib.util.spec_from_file_location("ai_message_memory_dedup_index_0091", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _action_index_migration_module():
    spec = importlib.util.spec_from_file_location("ai_message_memory_action_id_index_0092", ACTION_INDEX_MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("action id index migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    def execute(self, statement):
        self.events.append(str(statement))


def test_message_memory_model_declares_tenant_window_index_in_query_order() -> None:
    index = next(
        item for item in AiGroupMessageMemory.__table__.indexes
        if item.name == INDEX_NAME
    )

    assert tuple(str(expression) for expression in index.expressions) == (
        "ai_group_message_memory.tenant_id",
        "ai_group_message_memory.status",
        "planned_at DESC",
    )


def test_message_memory_model_declares_nonunique_action_id_index() -> None:
    index = next(item for item in AiGroupMessageMemory.__table__.indexes if item.name == ACTION_INDEX_NAME)

    assert tuple(str(expression) for expression in index.expressions) == (
        "ai_group_message_memory.action_id",
    )
    assert index.unique is False


def test_action_id_index_migration_upgrades_and_downgrades_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata = MetaData()
    Table(
        "ai_group_message_memory",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("action_id", String(36), nullable=False),
    )
    metadata.create_all(engine)
    migration = _action_index_migration_module()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        upgraded = {item["name"]: item for item in inspect(connection).get_indexes("ai_group_message_memory")}
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
        downgraded = {item["name"] for item in inspect(connection).get_indexes("ai_group_message_memory")}

    assert upgraded[ACTION_INDEX_NAME]["unique"] == 0
    assert ACTION_INDEX_NAME not in downgraded


@pytest.mark.parametrize("operation_name", ["upgrade", "downgrade"])
def test_action_id_index_migration_requires_target_table(operation_name: str) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    migration = _action_index_migration_module()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match="^required table missing: ai_group_message_memory$"):
            getattr(migration, operation_name)()


def test_action_id_index_postgres_ddl_is_concurrent_and_nonunique(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _action_index_migration_module()
    events: list[str] = []
    monkeypatch.setattr(migration, "op", _RecordingPostgresOp(events))
    monkeypatch.setattr(migration, "_require_table", lambda: None)
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: set())

    migration.upgrade()

    assert events == [
        "enter_autocommit",
        f"CREATE INDEX CONCURRENTLY {ACTION_INDEX_NAME} ON ai_group_message_memory (action_id)",
        "exit_autocommit",
    ]
    events.clear()
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: {ACTION_INDEX_NAME})

    migration.downgrade()

    assert events == [
        "enter_autocommit",
        f"DROP INDEX CONCURRENTLY {ACTION_INDEX_NAME}",
        "exit_autocommit",
    ]


def test_message_memory_index_migration_declares_concurrent_postgres_contract() -> None:
    migration = _migration_module()
    source = MIGRATION_PATH.read_text()

    assert migration.revision == "0091_ai_memory_index"
    assert migration.down_revision == "0090_ai_group_fallback"
    assert "autocommit_block" in source
    assert "CREATE INDEX CONCURRENTLY" in source
    assert "DROP INDEX CONCURRENTLY" in source
    assert INDEX_NAME in source


def test_message_memory_index_migration_upgrades_and_downgrades_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata = MetaData()
    Table(
        "ai_group_message_memory",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("tenant_id", Integer, nullable=False),
        Column("status", String(40), nullable=False),
        Column("planned_at", DateTime, nullable=False),
    )
    metadata.create_all(engine)
    migration = _migration_module()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        upgraded_indexes = {item["name"] for item in inspect(connection).get_indexes("ai_group_message_memory")}
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
        downgraded_indexes = {item["name"] for item in inspect(connection).get_indexes("ai_group_message_memory")}

    assert INDEX_NAME in upgraded_indexes
    assert INDEX_NAME not in downgraded_indexes


def test_message_memory_index_upgrade_fails_when_target_table_is_missing() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    migration = _migration_module()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match="^required table missing: ai_group_message_memory$"):
            migration.upgrade()


def test_message_memory_index_downgrade_fails_when_target_table_is_missing() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    migration = _migration_module()

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match="^required table missing: ai_group_message_memory$"):
            migration.downgrade()


@pytest.mark.parametrize("operation_name", ["upgrade", "downgrade"])
def test_postgres_migration_rejects_missing_required_table(
    monkeypatch: pytest.MonkeyPatch,
    operation_name: str,
) -> None:
    migration = _migration_module()
    events: list[str] = []

    class MissingTableInspector:
        @staticmethod
        def get_table_names():
            return ["other_table"]

    monkeypatch.setattr(migration, "op", _RecordingPostgresOp(events))
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: MissingTableInspector())
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: set())

    with pytest.raises(RuntimeError, match="^required table missing: ai_group_message_memory$"):
        getattr(migration, operation_name)()

    assert events == []


def test_postgres_concurrent_ddl_runs_inside_autocommit_block(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration_module()
    upgrade_events: list[str] = []
    monkeypatch.setattr(migration, "op", _RecordingPostgresOp(upgrade_events))
    monkeypatch.setattr(migration, "_require_table", lambda: None, raising=False)
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: set())

    migration.upgrade()

    assert upgrade_events == [
        "enter_autocommit",
        f"CREATE INDEX CONCURRENTLY {INDEX_NAME} ON ai_group_message_memory "
        "(tenant_id, status, planned_at DESC)",
        "exit_autocommit",
    ]

    downgrade_events: list[str] = []
    monkeypatch.setattr(migration, "op", _RecordingPostgresOp(downgrade_events))
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: {INDEX_NAME})

    migration.downgrade()

    assert downgrade_events == [
        "enter_autocommit",
        f"DROP INDEX CONCURRENTLY {INDEX_NAME}",
        "exit_autocommit",
    ]


def test_postgres_index_catalog_distinguishes_valid_and_invalid_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration_module()
    calls: list[tuple[str, dict[str, str]]] = []

    class FakeResult:
        def __init__(self, values: tuple[str, ...]):
            self.values = values

        def scalars(self):
            return self.values

    class FakeBind:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        @staticmethod
        def execute(statement, parameters):
            sql = str(statement)
            calls.append((sql, parameters))
            if "index_meta.indisvalid" in sql:
                return FakeResult(("valid_index",))
            return FakeResult(("valid_index", INDEX_NAME))

    fake_op = type("FakeOp", (), {"get_bind": staticmethod(lambda: FakeBind())})()
    monkeypatch.setattr(migration, "op", fake_op)

    valid_names = migration._index_names()
    all_names = migration._index_names(valid_only=False)

    assert valid_names == {"valid_index"}
    assert all_names == {"valid_index", INDEX_NAME}
    assert calls[0][1] == {"table_name": migration.TABLE_NAME}
    assert "index_meta.indisvalid" in calls[0][0]
    assert calls[1][1] == {"table_name": migration.TABLE_NAME}
    assert "index_meta.indisvalid" not in calls[1][0]


def test_postgres_index_migration_preserves_explicit_idempotency(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration_module()
    events: list[str] = []
    monkeypatch.setattr(migration, "op", _RecordingPostgresOp(events))
    monkeypatch.setattr(migration, "_require_table", lambda: None, raising=False)
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: {INDEX_NAME})

    migration.upgrade()

    assert events == []

    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: set())

    migration.downgrade()

    assert events == []


@pytest.mark.parametrize("migration_factory", [_migration_module, _action_index_migration_module])
def test_postgres_index_ddl_failure_propagates(monkeypatch: pytest.MonkeyPatch, migration_factory) -> None:
    migration = migration_factory()
    failure = RuntimeError("postgres index build failed")

    class FakeBind:
        dialect = type("Dialect", (), {"name": "postgresql"})()

    class FakeContext:
        @contextmanager
        def autocommit_block(self):
            yield

    class FakeOp:
        @staticmethod
        def get_bind():
            return FakeBind()

        @staticmethod
        def get_context():
            return FakeContext()

        @staticmethod
        def execute(_statement):
            raise failure

    monkeypatch.setattr(migration, "op", FakeOp())
    monkeypatch.setattr(migration, "_require_table", lambda: None, raising=False)
    monkeypatch.setattr(migration, "_index_names", lambda: set())

    with pytest.raises(RuntimeError) as exc_info:
        migration.upgrade()

    assert exc_info.value is failure


def test_window_memories_projects_only_similarity_columns_across_groups() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    statements: list[str] = []

    def capture_select(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "from ai_group_message_memory" in statement.lower():
            statements.append(statement)

    now = _now()
    with Session(engine) as session:
        session.add(
            AiGroupMessageMemory(
                tenant_id=1,
                group_id=22,
                status="success",
                planned_at=now,
                normalized_text="跨群语义",
                raw_text="跨群语义原文",
                result={"large": "x" * 4096},
            )
        )
        session.commit()
        event.listen(engine, "before_cursor_execute", capture_select)
        try:
            rows = _window_memories(
                session, tenant_id=1, group_id=999, cutoff=now - timedelta(days=7),
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture_select)

    assert len(rows) == 1
    assert rows[0].id
    assert rows[0].normalized_text == "跨群语义"
    assert rows[0].raw_text == "跨群语义原文"
    assert len(statements) == 1
    statement = statements[0].lower()
    select_clause = statement.split("from ai_group_message_memory", maxsplit=1)[0]
    assert [column.strip() for column in select_clause.removeprefix("select ").split(",")] == [
        "ai_group_message_memory.id",
        "ai_group_message_memory.normalized_text",
        "ai_group_message_memory.raw_text",
        "ai_group_message_memory.planned_at",
        "ai_group_message_memory.status",
    ]
    assert "ai_group_message_memory.result" not in statement
    assert "ai_group_message_memory.group_id" not in statement


def test_window_memories_propagates_database_errors() -> None:
    class UnavailableSession:
        def execute(self, _statement):
            raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="^database unavailable$"):
        _window_memories(
            UnavailableSession(), tenant_id=1, group_id=999, cutoff=_now() - timedelta(days=7),
        )


def test_historical_backfill_candidates_skip_poisoned_oldest_actions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now = _now()
    statements: list[str] = []
    actions = [
        Action(
            id=f"poisoned-action-{index:03d}",
            tenant_id=1,
            task_id="task-ai-memory",
            task_type="group_ai_chat",
            action_type="send_message",
            status="success",
            scheduled_at=now - timedelta(days=1),
            created_at=now + timedelta(seconds=index),
            payload={"group_id": 22, "message_text": f"历史消息 {index}"},
        )
        for index in range(POISONED_ACTION_COUNT + 1)
    ]
    memories = [
        AiGroupMessageMemory(
            tenant_id=1,
            group_id=22,
            action_id=action.id,
            status="success",
            planned_at=now - timedelta(days=1),
        )
        for action in actions[:POISONED_ACTION_COUNT]
    ]

    def capture_select(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "from actions" in statement.lower():
            statements.append(statement.lower())

    with Session(engine) as session:
        session.add_all([*actions, *memories])
        session.commit()
        event.listen(engine, "before_cursor_execute", capture_select)
        try:
            candidates = _historical_group_ai_actions(
                session, tenant_id=1, now=now, limit=POISONED_ACTION_COUNT,
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture_select)

    assert [action.id for action in candidates] == ["poisoned-action-100"]
    assert len(statements) == 1
    assert "not (exists" in statements[0] or "not exists" in statements[0]
    assert statements[0].count("from actions") == 2
