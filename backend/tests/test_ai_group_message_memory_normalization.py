from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.services._common import _now
from app.services.task_center.ai_message_memory import (
    DuplicateMessageReservation,
    mark_group_ai_message_result,
    normalize_group_ai_text,
    reserve_group_ai_message,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_normalization_collapses_variable_teacher_names_for_dedupe():
    assert normalize_group_ai_text("花花老师身材服务真好") == normalize_group_ai_text("小雪老师身材服务真好")


def test_teacher_name_variants_block_same_shell_days_later():
    now = _now()
    with _session() as session:
        first = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=22,
            task_id="task-1",
            account_id=101,
            raw_text="花花老师身材服务真好",
            now=now,
        )
        mark_group_ai_message_result(session, first.id, status="success", action_id="action-1")
        session.commit()

        with pytest.raises(DuplicateMessageReservation) as exc:
            reserve_group_ai_message(
                session,
                tenant_id=1,
                group_id=22,
                task_id="task-1",
                account_id=102,
                raw_text="小雪老师身材服务真好",
                now=now + timedelta(days=2),
            )

        assert exc.value.reference_id == first.id
        assert exc.value.duplicate_window == "7d_semantic"
