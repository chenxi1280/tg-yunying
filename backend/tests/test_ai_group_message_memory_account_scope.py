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


def test_other_account_history_never_hard_blocks() -> None:
    now = _now()
    with _session() as session:
        reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=21,
            task_id="task-a",
            account_id=101,
            raw_text="今天先看看价格再决定",
            now=now,
        )
        session.commit()

        memory = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=22,
            task_id="task-b",
            account_id=102,
            raw_text="今天先看看价格再决定",
            now=now + timedelta(minutes=1),
        )

        assert memory.account_id == 102


def test_other_account_exact_duplicate_in_same_group_window_is_blocked() -> None:
    now = _now()
    with _session() as session:
        first = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=21,
            task_id="task-a",
            account_id=101,
            raw_text="今天先看看价格再决定",
            now=now,
        )
        session.commit()

        with pytest.raises(DuplicateMessageReservation) as exc:
            reserve_group_ai_message(
                session,
                tenant_id=1,
                group_id=21,
                task_id="task-b",
                account_id=102,
                raw_text="今天先看看价格再决定！！",
                now=now + timedelta(minutes=1),
            )

        assert exc.value.reference_id == first.id
        assert exc.value.duplicate_window == "5m_group_exact"


def test_same_account_history_blocks_across_tasks_groups_and_mask_versions() -> None:
    now = _now()
    with _session() as session:
        first = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=21,
            task_id="task-a",
            account_id=101,
            raw_text="主任这个可以先问价格",
            now=now,
            profile_version=1,
        )
        session.commit()

        with pytest.raises(DuplicateMessageReservation) as exc:
            reserve_group_ai_message(
                session,
                tenant_id=1,
                group_id=22,
                task_id="task-b",
                account_id=101,
                raw_text="主任这个可以先问价格",
                now=now + timedelta(days=9),
                profile_version=2,
            )

        assert exc.value.reference_id == first.id
        assert exc.value.duplicate_window == "10d_exact"


def test_same_account_history_expires_after_ten_days() -> None:
    now = _now()
    with _session() as session:
        reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=21,
            task_id="task-a",
            account_id=101,
            raw_text="十天前的旧内容",
            now=now,
        )
        session.commit()

        memory = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=22,
            task_id="task-b",
            account_id=101,
            raw_text="十天前的旧内容",
            now=now + timedelta(days=10, seconds=1),
        )

        assert memory.account_id == 101


def test_memory_freezes_account_mask_evidence() -> None:
    with _session() as session:
        memory = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=21,
            task_id="task-a",
            account_id=101,
            raw_text="按自己的口吻说一句",
            account_mask_id="mask-101-v3",
            account_mask_version=3,
            mask_contract_version="style_only_v2",
            mask_snapshot_hash="snapshot-hash",
        )

        assert memory.account_mask_id == "mask-101-v3"
        assert memory.account_mask_version == 3
        assert memory.mask_contract_version == "style_only_v2"
        assert memory.mask_snapshot_hash == "snapshot-hash"
