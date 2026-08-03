from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, AiGroupMessageMemory, Task, TaskAccountDailyCoverage
from app.services._common import _now
from app.services.task_center.daily_coverage import release_voice_profile_coverage_for_check_in
from app.services.task_center.direct_check_in import (
    DUE_CATCH_UP_CHECK_IN_SOURCE,
    MASK_MISSING_CHECK_IN_SOURCE,
    due_catch_up_check_in_memory_is_valid,
    reserve_due_catch_up_check_in_memory,
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


def test_due_catch_up_check_in_uses_action_bound_memory_despite_prior_text() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    with Session(engine) as session:
        task = Task(id="task-catch-up", tenant_id=1, name="AI", type="group_ai_chat")
        action = Action(
            id="action-catch-up",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=101,
            status="executing",
            scheduled_at=now_value,
            payload={},
        )
        coverage = TaskAccountDailyCoverage(
            id="coverage-catch-up",
            tenant_id=1,
            task_id=task.id,
            group_id=21,
            account_id=101,
            coverage_date=now_value.date(),
            state="reserved",
            reserved_action_id=action.id,
            targeted_at=now_value,
        )
        prior = AiGroupMessageMemory(
            tenant_id=1,
            group_id=21,
            task_id=task.id,
            action_id="prior-action",
            account_id=101,
            raw_text="签到",
            normalized_text="签到",
            text_fingerprint="prior",
            status="success",
            planned_at=now_value,
        )
        session.add_all([task, action, coverage, prior])
        session.flush()
        data = _due_catch_up_data()
        payload = SendMessagePayload.model_validate(data)

        memory = reserve_due_catch_up_check_in_memory(
            session,
            action,
            payload,
            data=data,
        )
        data["ai_message_memory_id"] = memory.id
        checked = SendMessagePayload.model_validate(data)

        assert memory.action_id == action.id
        assert memory.content_source == DUE_CATCH_UP_CHECK_IN_SOURCE
        assert due_catch_up_check_in_memory_is_valid(session, action, checked)


def test_due_catch_up_check_in_rejects_incomplete_contract() -> None:
    data = _due_catch_up_data()
    data["primary_quantity_slot_id"] = ""

    from app.services.task_center.direct_check_in import is_due_catch_up_check_in

    assert is_due_catch_up_check_in(data) is False


def _due_catch_up_data() -> dict:
    return {
        "group_id": 21,
        "message_text": "签到",
        "ai_generation_status": "ready",
        "coverage_ledger_id": "coverage-catch-up",
        "daily_group_target_id": "daily-target-catch-up",
        "primary_quantity_slot_id": "quantity-slot-catch-up",
        "account_mask_id": "mask-catch-up",
        "account_mask_version": 2,
        "account_mask_snapshot_hash": "snapshot-catch-up",
        "mask_status": "active",
        "content_source": DUE_CATCH_UP_CHECK_IN_SOURCE,
        "generation_source": "static_safe_fallback",
        "quality_fallback": "check_in_fallback",
        "fallback_reason": "due_catch_up_provider_budget_exhausted",
    }
