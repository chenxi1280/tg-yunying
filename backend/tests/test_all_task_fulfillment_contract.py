from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AppUser,
    ContentMixContract,
    ContentMixCycle,
    ContentMixCycleSlot,
    CommentFulfillmentObligation,
    Action,
    PendingVisibilityCredit,
    SearchClickFulfillmentObligation,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    TaskStartOperation,
    Tenant,
)


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="单用户"))
        current.add(
            AppUser(
                id=7,
                tenant_id=1,
                name="运营",
                role="admin",
                email="operator@example.com",
            )
        )
        current.flush()
        yield current


def _task(*, task_id: str, request_id: str) -> Task:
    return Task(
        id=task_id,
        tenant_id=1,
        name="AI 活群",
        type="group_ai_chat",
        created_by_user_id=7,
        create_task_type="group_ai_chat",
        client_request_id=request_id,
        request_fingerprint=f"fingerprint:{request_id}",
    )


def test_create_idempotency_key_is_not_released_by_soft_delete(session: Session) -> None:
    original = _task(task_id="task-1", request_id="request-1")
    original.deleted_at = datetime.now(timezone.utc)
    session.add(original)
    session.commit()

    session.add(_task(task_id="task-2", request_id="request-1"))

    with pytest.raises(IntegrityError):
        session.commit()


def test_ai_quantity_slot_can_belong_to_only_one_cycle_slot(session: Session) -> None:
    task = _task(task_id="task-cycle", request_id="request-cycle")
    ledger = TaskDayLedger(
        id="ledger-1",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 7, 29),
        period_start_at=datetime(2026, 7, 28, 16, tzinfo=timezone.utc),
        deadline_at=datetime(2026, 7, 29, 16, tzinfo=timezone.utc),
        day_phase="full_day_committed",
        planning_anchor_at=datetime(2026, 7, 28, 16, tzinfo=timezone.utc),
    )
    quantity_slot = TaskGroupDailyMessageSlot(
        id="quantity-slot-1",
        tenant_id=1,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        target_operation_target_id=11,
        slot_kind="extra_volume",
        slot_ordinal=1,
    )
    session.add_all([task, ledger, quantity_slot])
    session.flush()

    for sequence in (1, 2):
        cycle = ContentMixCycle(
            id=f"cycle-{sequence}",
            tenant_id=1,
            task_id=task.id,
            target_operation_target_id=11,
            task_day_ledger_id=ledger.id,
            cycle_seq=sequence,
            config_revision=1,
            scope_total_slots=1,
            allocation_seed=f"seed-{sequence}",
            allocation_closed_at=datetime.now(timezone.utc),
        )
        contract = ContentMixContract(
            id=f"contract-{sequence}",
            tenant_id=1,
            content_mix_scope_key=f"ai:{task.id}:11:{cycle.id}:1",
            content_contract_version=1,
            scope_total_slots=1,
            allocation_seed=f"seed-{sequence}",
            reply_min_required_count=0,
            reply_planned_count=0,
            direct_planned_count=1,
        )
        session.add_all([cycle, contract])
        session.flush()
        session.add(
            ContentMixCycleSlot(
                id=f"cycle-slot-{sequence}",
                tenant_id=1,
                cycle_id=cycle.id,
                slot_index=1,
                primary_quantity_slot_id=quantity_slot.id,
                relation_kind="direct",
            )
        )
        if sequence == 1:
            session.commit()

    with pytest.raises(IntegrityError):
        session.commit()


def test_start_operation_is_current_state_not_attempt_history(session: Session) -> None:
    task = _task(task_id="task-start", request_id="request-start")
    session.add(task)
    session.flush()
    session.add_all(
        [
            TaskStartOperation(
                task_id=task.id,
                start_operation_id="start-1",
                operation_version=1,
                requested_by_user_id=7,
                source="explicit_start",
                status="failed",
            ),
            TaskStartOperation(
                task_id=task.id,
                start_operation_id="start-2",
                operation_version=2,
                requested_by_user_id=7,
                source="explicit_start",
                status="processing",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_non_ai_obligations_use_natural_keys_not_ai_quantity_slots(
    session: Session,
) -> None:
    assert not hasattr(CommentFulfillmentObligation, "primary_quantity_slot_id")
    assert not hasattr(SearchClickFulfillmentObligation, "primary_quantity_slot_id")

    task = _task(task_id="task-comment", request_id="request-comment")
    session.add(task)
    session.flush()
    session.add(
        CommentFulfillmentObligation(
            id="comment-1",
            tenant_id=1,
            task_id=task.id,
            channel_message_id=88,
            comment_plan_revision=1,
            target_ordinal=1,
            status="confirmed",
            telegram_discussion_peer_id="-10088",
            remote_comment_id="9001",
        )
    )
    session.commit()
    session.add(
        CommentFulfillmentObligation(
            id="comment-2",
            tenant_id=1,
            task_id=task.id,
            channel_message_id=88,
            comment_plan_revision=1,
            target_ordinal=2,
            status="confirmed",
            telegram_discussion_peer_id="-10088",
            remote_comment_id="9001",
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_pending_visibility_holds_one_ai_slot_without_confirming_it(
    session: Session,
) -> None:
    task = _task(task_id="task-hold", request_id="request-hold")
    ledger = TaskDayLedger(
        id="ledger-hold",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 7, 29),
        period_start_at=datetime(2026, 7, 28, 16, tzinfo=timezone.utc),
        deadline_at=datetime(2026, 7, 29, 16, tzinfo=timezone.utc),
        day_phase="full_day_committed",
        planning_anchor_at=datetime(2026, 7, 28, 16, tzinfo=timezone.utc),
    )
    quantity_slot = TaskGroupDailyMessageSlot(
        id="quantity-hold",
        tenant_id=1,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        target_operation_target_id=11,
        slot_kind="extra_volume",
        slot_ordinal=1,
    )
    actions = [
        Action(
            id=f"hold-action-{index}",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
        )
        for index in (1, 2)
    ]
    session.add_all([task, ledger, quantity_slot, *actions])
    session.flush()
    session.add(
        PendingVisibilityCredit(
            tenant_id=1,
            action_id=actions[0].id,
            task_day_ledger_id=ledger.id,
            primary_quantity_slot_id=quantity_slot.id,
            remote_message_id="remote-1",
            admission_version=3,
            status="open",
        )
    )
    session.commit()
    session.add(
        PendingVisibilityCredit(
            tenant_id=1,
            action_id=actions[1].id,
            task_day_ledger_id=ledger.id,
            primary_quantity_slot_id=quantity_slot.id,
            remote_message_id="remote-2",
            admission_version=3,
            status="unknown",
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()
