from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Task, TaskAccountDailyCoverage
from app.services.task_center.daily_coverage import release_voice_profile_coverage_for_check_in
from app.services.task_center.direct_check_in import (
    MASK_MISSING_CHECK_IN_SOURCE,
    requires_direct_check_in,
)
from app.services.task_center.payloads import SendMessagePayload


pytestmark = pytest.mark.no_postgres


def _payload(**updates) -> SendMessagePayload:
    return SendMessagePayload(
        group_id=21,
        ai_generation_status="pending",
        coverage_ledger_id="coverage-1",
        **updates,
    )


def test_normal_coverage_with_mask_uses_ai_generation() -> None:
    payload = _payload(
        content_source="account_mask",
        account_mask_id="mask-1",
        account_mask_version=3,
    )

    assert requires_direct_check_in(payload) is False


def test_missing_mask_coverage_uses_exact_check_in_fallback() -> None:
    payload = _payload(
        content_source=MASK_MISSING_CHECK_IN_SOURCE,
        mask_status="missing",
        fallback_obligation_key="task:21:101:2026-07-28:mask_missing_check_in",
    )

    assert requires_direct_check_in(payload) is True


def test_missing_mask_check_in_cannot_fill_group_total_extra() -> None:
    payload = SendMessagePayload(
        group_id=21,
        ai_generation_status="pending",
        content_source=MASK_MISSING_CHECK_IN_SOURCE,
        mask_status="missing",
    )

    assert requires_direct_check_in(payload) is False


def test_missing_mask_blocker_is_released_for_check_in_coverage() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task = Task(id="task-1", tenant_id=1, name="AI", type="group_ai_chat")
        row = TaskAccountDailyCoverage(
            id="coverage-1",
            tenant_id=1,
            task_id=task.id,
            group_id=21,
            account_id=101,
            coverage_date=date(2026, 7, 28),
            target_count=1,
            confirmed_count=0,
            state="blocked",
            blocker_code="voice_profile_missing",
        )
        session.add_all([task, row])
        session.flush()

        released = release_voice_profile_coverage_for_check_in(
            session,
            task,
            now=datetime(2026, 7, 28, 10, 0),
        )

        assert released == 1
        assert row.state == "ready"
        assert row.recovery_path == "mask_missing_check_in"
