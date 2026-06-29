from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.services._common import _now
from app.services.task_center.ai_message_memory import (
    DuplicateMessageReservation,
    reserve_group_ai_message,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_group_ai_message_memory_blocks_exact_duplicate_across_groups():
    now = _now()
    with _session() as session:
        first = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=22,
            task_id="task-1",
            account_id=101,
            raw_text="嫩是真嫩 就是不知道稳不稳",
            now=now,
        )
        session.commit()

        with pytest.raises(DuplicateMessageReservation) as exc:
            reserve_group_ai_message(
                session,
                tenant_id=1,
                group_id=33,
                task_id="task-2",
                account_id=102,
                raw_text="嫩是真嫩 就是不知道稳不稳",
                now=now + timedelta(minutes=1),
            )

        assert exc.value.reference_id == first.id
        assert exc.value.duplicate_window == "5m_exact"


def test_group_ai_message_memory_blocks_semantic_duplicate_across_groups():
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
        session.commit()

        with pytest.raises(DuplicateMessageReservation) as exc:
            reserve_group_ai_message(
                session,
                tenant_id=1,
                group_id=33,
                task_id="task-2",
                account_id=102,
                raw_text="花花老师服务身材挺好",
                now=now + timedelta(days=1),
            )

        assert exc.value.reference_id == first.id
        assert exc.value.duplicate_window == "7d_semantic"
