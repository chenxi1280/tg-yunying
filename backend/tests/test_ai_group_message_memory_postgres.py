from __future__ import annotations

from datetime import datetime, timedelta
from time import perf_counter

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models import Action, AiGroupMessageMemory, Task, Tenant
from app.services._common import _now
from app.services.task_center.ai_message_memory import (
    _first_similar_memory,
    _memory_exists_for_action,
    _window_memories,
    backfill_group_ai_message_memory_from_actions,
)


TEST_TENANT_ID = 913_715
ROW_COUNT = 40_741
BATCH_SIZE = 2_000
MAX_QUERY_SECONDS = 2.0
MAX_SCAN_SECONDS = 5.0
MAX_LOOKUP_SECONDS = 0.1
MAX_BACKFILL_SECONDS = 10.0
SIMILARITY_THRESHOLD = 0.99
LARGE_RESULT_TEXT = "message-memory-history" * 64
GROUP_ID_BASE = 10_000
GROUP_COUNT = 4
TARGET_GROUP_ID = 999
WINDOW_DAYS = 7
WINDOW_SECONDS = int(timedelta(days=WINDOW_DAYS).total_seconds())
CANDIDATE_TEXT = "完全不相似的唯一候选文本"
EXPECTED_ROW_KEYS = {"id", "normalized_text", "raw_text"}
ACTION_INDEX_NAME = "ix_ai_group_message_memory_action_id"
BACKFILL_ACTION_COUNT = 200
POISONED_ACTION_COUNT = 100
TEST_TASK_ID = "pg-ai-memory-backfill-task"
DUPLICATE_ACTION_ID = "pg-backfill-action-000"
DUPLICATE_ACTION_INDEX = ROW_COUNT - 1


def _cleanup_memory_rows() -> None:
    with SessionLocal() as session:
        session.execute(delete(Action).where(Action.tenant_id == TEST_TENANT_ID))
        session.execute(delete(Task).where(Task.tenant_id == TEST_TENANT_ID))
        session.execute(
            delete(AiGroupMessageMemory).where(AiGroupMessageMemory.tenant_id == TEST_TENANT_ID)
        )
        session.execute(delete(Tenant).where(Tenant.id == TEST_TENANT_ID))
        session.commit()


def _memory_rows(now_value: datetime, start: int, stop: int) -> list[dict]:
    return [
        {
            "id": f"pg-ai-memory-{index:05d}",
            "tenant_id": TEST_TENANT_ID,
            "group_id": GROUP_ID_BASE + index % GROUP_COUNT,
            "raw_text": f"历史活群消息{index:05d}",
            "normalized_text": f"历史活群消息{index:05d}",
            "action_id": _seed_action_id(index),
            "status": "success",
            "planned_at": now_value - timedelta(seconds=index % WINDOW_SECONDS),
            "result": {"history": LARGE_RESULT_TEXT},
        }
        for index in range(start, stop)
    ]


def _seed_action_id(index: int) -> str:
    if index == DUPLICATE_ACTION_INDEX:
        return DUPLICATE_ACTION_ID
    if index < POISONED_ACTION_COUNT:
        return f"pg-backfill-action-{index:03d}"
    return f"pg-memory-action-{index:05d}"


def _seed_memory_rows(now_value: datetime) -> None:
    with SessionLocal() as session:
        session.add(Tenant(id=TEST_TENANT_ID, name="AI message memory postgres scale"))
        session.commit()
        for start in range(0, ROW_COUNT, BATCH_SIZE):
            stop = min(start + BATCH_SIZE, ROW_COUNT)
            session.execute(insert(AiGroupMessageMemory), _memory_rows(now_value, start, stop))
        session.commit()


def _seed_backfill_actions(now_value: datetime) -> None:
    action_time = now_value - timedelta(days=1)
    task_row = {
        "id": TEST_TASK_ID,
        "tenant_id": TEST_TENANT_ID,
        "name": "AI memory backfill scale",
        "type": "group_ai_chat",
        "status": "running",
    }
    action_rows = [
        {
            "id": f"pg-backfill-action-{index:03d}",
            "tenant_id": TEST_TENANT_ID,
            "task_id": TEST_TASK_ID,
            "task_type": "group_ai_chat",
            "action_type": "send_message",
            "status": "success",
            "scheduled_at": action_time,
            "executed_at": action_time,
            "created_at": action_time + timedelta(seconds=index),
            "payload": {"group_id": 22, "message_text": f"待回填历史消息 {index}"},
        }
        for index in range(BACKFILL_ACTION_COUNT)
    ]
    with SessionLocal() as session:
        session.execute(insert(Task), [task_row])
        session.execute(insert(Action), action_rows)
        session.commit()


def _assert_postgres_seed(session: Session) -> None:
    assert session.get_bind().dialect.name == "postgresql"
    stored_result = session.scalar(
        select(AiGroupMessageMemory.result)
        .where(AiGroupMessageMemory.tenant_id == TEST_TENANT_ID)
        .limit(1)
    )
    assert stored_result == {"history": LARGE_RESULT_TEXT}


def _measure_window(session: Session, now_value: datetime) -> tuple[list[Row], float]:
    started_at = perf_counter()
    rows = _window_memories(
        session,
        tenant_id=TEST_TENANT_ID,
        group_id=TARGET_GROUP_ID,
        cutoff=now_value - timedelta(days=WINDOW_DAYS),
    )
    return rows, perf_counter() - started_at


def _measure_scan(rows: list[Row]) -> tuple[Row | None, float]:
    started_at = perf_counter()
    duplicate = _first_similar_memory(rows, CANDIDATE_TEXT, SIMILARITY_THRESHOLD)
    return duplicate, perf_counter() - started_at


def _measure_action_lookup(session: Session, action_id: str) -> tuple[bool, float]:
    started_at = perf_counter()
    found = _memory_exists_for_action(session, action_id)
    return found, perf_counter() - started_at


def _plan_index_names(node: object) -> set[str]:
    if isinstance(node, list):
        return set().union(*(_plan_index_names(item) for item in node))
    if not isinstance(node, dict):
        return set()
    names = {str(node["Index Name"])} if node.get("Index Name") else set()
    return names | set().union(*(_plan_index_names(value) for value in node.values()))


def _assert_action_lookup_performance(session: Session) -> None:
    existing, existing_elapsed = _measure_action_lookup(session, DUPLICATE_ACTION_ID)
    missing, missing_elapsed = _measure_action_lookup(session, "pg-backfill-action-100")
    plan = session.scalar(
        text(
            "EXPLAIN (ANALYZE, FORMAT JSON) "
            "SELECT id FROM ai_group_message_memory WHERE action_id = :action_id LIMIT 1"
        ),
        {"action_id": DUPLICATE_ACTION_ID},
    )

    assert existing is True
    assert missing is False
    assert existing_elapsed < MAX_LOOKUP_SECONDS
    assert missing_elapsed < MAX_LOOKUP_SECONDS
    assert ACTION_INDEX_NAME in _plan_index_names(plan)


def _assert_backfill_batch_performance(session: Session, now_value: datetime) -> None:
    started_at = perf_counter()
    result = backfill_group_ai_message_memory_from_actions(
        session,
        tenant_id=TEST_TENANT_ID,
        now=now_value,
        limit=POISONED_ACTION_COUNT,
    )
    elapsed = perf_counter() - started_at
    steady_started_at = perf_counter()
    steady_result = backfill_group_ai_message_memory_from_actions(
        session,
        tenant_id=TEST_TENANT_ID,
        now=now_value,
        limit=POISONED_ACTION_COUNT,
    )
    steady_elapsed = perf_counter() - steady_started_at
    created = session.scalar(
        select(func.count(AiGroupMessageMemory.id)).where(
            AiGroupMessageMemory.tenant_id == TEST_TENANT_ID,
            AiGroupMessageMemory.action_id.in_(
                [f"pg-backfill-action-{index:03d}" for index in range(POISONED_ACTION_COUNT, BACKFILL_ACTION_COUNT)]
            ),
        )
    )
    duplicate_count = session.scalar(
        select(func.count(AiGroupMessageMemory.id)).where(
            AiGroupMessageMemory.tenant_id == TEST_TENANT_ID,
            AiGroupMessageMemory.action_id == DUPLICATE_ACTION_ID,
        )
    )

    assert result == {"created": POISONED_ACTION_COUNT, "skipped_existing": 0, "skipped_invalid": 0}
    assert steady_result == {"created": 0, "skipped_existing": 0, "skipped_invalid": 0}
    assert created == POISONED_ACTION_COUNT
    assert duplicate_count == 2
    assert elapsed < MAX_BACKFILL_SECONDS
    assert steady_elapsed < MAX_BACKFILL_SECONDS


def test_ai_message_memory_postgres_scales_at_production_volume() -> None:
    Base.metadata.create_all(engine)
    _cleanup_memory_rows()
    now_value = _now()

    try:
        _seed_memory_rows(now_value)
        _seed_backfill_actions(now_value)
        with SessionLocal() as session:
            _assert_postgres_seed(session)
            rows, query_elapsed = _measure_window(session, now_value)
            duplicate, scan_elapsed = _measure_scan(rows)
            _assert_action_lookup_performance(session)
            _assert_backfill_batch_performance(session, now_value)

        assert len(rows) == ROW_COUNT
        assert set(rows[0]._mapping) == EXPECTED_ROW_KEYS
        assert duplicate is None
        assert query_elapsed < MAX_QUERY_SECONDS, (
            f"AI message memory PostgreSQL query took {query_elapsed:.3f}s; "
            f"limit is {MAX_QUERY_SECONDS:.3f}s"
        )
        assert scan_elapsed < MAX_SCAN_SECONDS, (
            f"AI message memory no-match scan took {scan_elapsed:.3f}s; "
            f"limit is {MAX_SCAN_SECONDS:.3f}s"
        )
    finally:
        _cleanup_memory_rows()
