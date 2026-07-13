from __future__ import annotations

from datetime import datetime, timedelta
from time import perf_counter

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.models import AiGroupMessageMemory, Tenant
from app.services._common import _now
from app.services.task_center.ai_message_memory import _first_similar_memory, _window_memories


TEST_TENANT_ID = 913_715
ROW_COUNT = 40_741
BATCH_SIZE = 2_000
MAX_QUERY_SECONDS = 2.0
MAX_SCAN_SECONDS = 5.0
SIMILARITY_THRESHOLD = 0.99
LARGE_RESULT_TEXT = "message-memory-history" * 64
GROUP_ID_BASE = 10_000
GROUP_COUNT = 4
TARGET_GROUP_ID = 999
WINDOW_DAYS = 7
WINDOW_SECONDS = int(timedelta(days=WINDOW_DAYS).total_seconds())
CANDIDATE_TEXT = "完全不相似的唯一候选文本"
EXPECTED_ROW_KEYS = {"id", "normalized_text", "raw_text"}


def _cleanup_memory_rows() -> None:
    with SessionLocal() as session:
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
            "status": "success",
            "planned_at": now_value - timedelta(seconds=index % WINDOW_SECONDS),
            "result": {"history": LARGE_RESULT_TEXT},
        }
        for index in range(start, stop)
    ]


def _seed_memory_rows(now_value: datetime) -> None:
    with SessionLocal() as session:
        session.add(Tenant(id=TEST_TENANT_ID, name="AI message memory postgres scale"))
        session.commit()
        for start in range(0, ROW_COUNT, BATCH_SIZE):
            stop = min(start + BATCH_SIZE, ROW_COUNT)
            session.execute(insert(AiGroupMessageMemory), _memory_rows(now_value, start, stop))
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


def test_ai_message_memory_postgres_scales_at_production_volume() -> None:
    Base.metadata.create_all(engine)
    _cleanup_memory_rows()
    now_value = _now()

    try:
        _seed_memory_rows(now_value)
        with SessionLocal() as session:
            _assert_postgres_seed(session)
            rows, query_elapsed = _measure_window(session, now_value)
            duplicate, scan_elapsed = _measure_scan(rows)

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
