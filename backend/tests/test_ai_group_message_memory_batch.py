from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, event, inspect
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AiGroupMessageMemory
from app.services._common import _now
from app.services.task_center.ai_message_memory import (
    DuplicateMessageReservation,
    reserve_group_ai_message,
)
from app.services.task_center.ai_message_memory_batch import DuplicateMemoryBatch


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0102_ai_memory_updated_index.py"
INDEX_NAME = "ix_ai_group_message_memory_tenant_status_updated"


def test_generation_batch_loads_history_once_and_uses_incremental_refresh() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now = _now()
    semantic_selects: list[str] = []

    def capture_select(_connection, _cursor, statement, _parameters, _context, _executemany):
        normalized = statement.lower()
        projected = "select ai_group_message_memory.id, ai_group_message_memory.normalized_text"
        if normalized.startswith(projected):
            semantic_selects.append(normalized)

    with Session(engine) as session:
        _seed_history(session, now)
        event.listen(engine, "before_cursor_execute", capture_select)
        try:
            batch = DuplicateMemoryBatch(now=now)
            _reserve_distinct_batch(session, batch)
            with pytest.raises(DuplicateMessageReservation) as exc_info:
                _reserve(session, batch, 99, "甲方今天讨论课程呢")
        finally:
            event.remove(engine, "before_cursor_execute", capture_select)

    full_scans = [sql for sql in semantic_selects if "updated_at" not in sql]
    incremental_scans = [sql for sql in semantic_selects if "updated_at" in sql]
    assert len(full_scans) == 1
    assert len(incremental_scans) == 3
    assert exc_info.value.duplicate_window in {"1h_similar", "7d_semantic"}


def test_batch_refresh_observes_memory_committed_after_initial_snapshot() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    batch = DuplicateMemoryBatch(now=_now())
    with Session(engine) as first:
        _reserve(first, batch, 1, "第一条无关消息")
        first.commit()
        with Session(engine) as concurrent:
            _reserve(concurrent, None, 2, "并发提交的语义消息")
            concurrent.commit()
        with pytest.raises(DuplicateMessageReservation) as exc_info:
            _reserve(first, batch, 3, "并发提交的语义消息呢")

    assert exc_info.value.duplicate_window in {"1h_similar", "7d_semantic"}


def test_batch_refresh_removes_memory_changed_to_inactive_status() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as first:
        _reserve(first, None, 1, "这条历史消息随后会失效")
        first.commit()
        batch = DuplicateMemoryBatch(now=_now())
        _reserve(first, batch, 2, "周末一起去公园放风筝")
        first.commit()
        with Session(engine) as concurrent:
            memory = concurrent.query(AiGroupMessageMemory).filter_by(account_id=1).one()
            inactive_memory_id = str(memory.id)
            memory.status = "failed"
            concurrent.commit()
        _reserve(first, batch, 3, "这条历史消息随后会失效呢")

    cached_ids = {str(row.id) for row in batch.rows_by_tenant[1]}
    assert inactive_memory_id not in cached_ids


def test_exact_duplicate_short_circuits_semantic_window_scan() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now = _now()
    semantic_selects: list[str] = []

    def capture_select(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lower().startswith(
            "select ai_group_message_memory.id, ai_group_message_memory.normalized_text"
        ):
            semantic_selects.append(statement)

    with Session(engine) as session:
        reserve_group_ai_message(
            session, tenant_id=1, group_id=22, task_id="task-exact", account_id=1,
            raw_text="完全相同的消息", now=now,
        )
        event.listen(engine, "before_cursor_execute", capture_select)
        try:
            with pytest.raises(DuplicateMessageReservation) as exc_info:
                reserve_group_ai_message(
                    session, tenant_id=1, group_id=33, task_id="task-exact", account_id=2,
                    raw_text="完全相同的消息", now=now,
                )
        finally:
            event.remove(engine, "before_cursor_execute", capture_select)

    assert exc_info.value.duplicate_window == "5m_exact"
    assert semantic_selects == []


def test_updated_index_migration_is_reversible_on_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata = MetaData()
    Table(
        "ai_group_message_memory", metadata,
        Column("id", String(36), primary_key=True),
        Column("tenant_id", Integer), Column("status", String(40)),
        Column("updated_at", DateTime),
    )
    metadata.create_all(engine)
    migration = _migration_module()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        upgraded = {item["name"] for item in inspect(connection).get_indexes("ai_group_message_memory")}
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
        downgraded = {item["name"] for item in inspect(connection).get_indexes("ai_group_message_memory")}

    assert INDEX_NAME in upgraded
    assert INDEX_NAME not in downgraded


def test_updated_index_postgres_ddl_is_concurrent_and_covering(monkeypatch) -> None:
    migration = _migration_module()
    events: list[str] = []
    monkeypatch.setattr(migration, "op", _RecordingPostgresOp(events))
    monkeypatch.setattr(migration, "_require_table", lambda: None)
    monkeypatch.setattr(migration, "_index_names", lambda **_kwargs: set())

    migration.upgrade()

    assert events == ["enter_autocommit", migration.POSTGRES_CREATE, "exit_autocommit"]
    assert "(tenant_id, updated_at DESC)" in migration.POSTGRES_CREATE
    assert "INCLUDE (status, planned_at, id, normalized_text, raw_text)" in migration.POSTGRES_CREATE


def _seed_history(session: Session, now) -> None:
    session.add(AiGroupMessageMemory(
        tenant_id=1, group_id=22, status="success", planned_at=now - timedelta(days=1),
        normalized_text="历史基线消息", raw_text="历史基线消息",
    ))
    session.commit()


def _reserve_distinct_batch(session: Session, batch: DuplicateMemoryBatch) -> None:
    for account_id, content in enumerate(
        ("甲方今天讨论课程", "乙方明天安排聚会", "丙方周末准备出游"), 1,
    ):
        _reserve(session, batch, account_id, content)


def _reserve(
    session: Session,
    batch: DuplicateMemoryBatch | None,
    account_id: int,
    content: str,
) -> None:
    reserve_group_ai_message(
        session, tenant_id=1, group_id=20 + account_id, task_id="task-batch",
        account_id=account_id, raw_text=content, duplicate_batch=batch,
    )


def _migration_module():
    spec = importlib.util.spec_from_file_location("ai_memory_updated_index_0102", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
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

        class Context:
            @contextmanager
            def autocommit_block(self):
                events.append("enter_autocommit")
                try:
                    yield
                finally:
                    events.append("exit_autocommit")

        return Context()

    def execute(self, statement):
        self.events.append(str(statement))
