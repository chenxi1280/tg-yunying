from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AuditLog,
    ContentMixContract,
    ContentMixCycleSlot,
    ContentMixObligation,
    ExecutionAttempt,
    OperationTarget,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    Tenant,
)
from app.services.task_center.ai_reply_ratio_recovery import (
    apply_reply_ratio_recovery,
    build_reply_ratio_recovery_snapshot,
    reply_ratio_recovery_state_hash,
)
from app.services.task_center.content_mix_cycles import (
    ContentMixCycleSpec,
    ContentMixSlotSpec,
    create_content_mix_cycle,
)


pytestmark = pytest.mark.no_postgres
TARGET_DATE = date(2026, 8, 3)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        _seed_replan_cycle(current)
        yield current


def test_recovery_reclassifies_only_guarded_excess_reply_slots(session: Session) -> None:
    snapshot = build_reply_ratio_recovery_snapshot(
        session,
        task_ids=("task-reply-recovery",),
        target_date=TARGET_DATE,
        per_task_limit=2,
    )
    task_state = snapshot["tasks"][0]
    ordered_slot_ids = list(session.scalars(
        select(ContentMixCycleSlot.id).order_by(ContentMixCycleSlot.slot_index),
    ))

    assert task_state["slot_total"] == 5
    assert task_state["reply_total"] == 5
    assert task_state["desired_reply_total"] == 1
    assert task_state["candidate_slot_ids"] == ordered_slot_ids[:2]

    result = apply_reply_ratio_recovery(
        session,
        snapshot=snapshot,
        expected_state_hash=reply_ratio_recovery_state_hash(snapshot),
        actor="incident:test",
    )
    slots = list(session.scalars(select(ContentMixCycleSlot).order_by(ContentMixCycleSlot.slot_index)))
    contract = session.scalar(select(ContentMixContract))

    assert result["changed_slot_count"] == 2
    assert [slot.relation_kind for slot in slots] == ["direct", "direct", "reply", "reply", "reply"]
    assert [slot.slot_state for slot in slots[:2]] == ["replan_required", "replan_required"]
    assert contract.reply_planned_count == 3
    assert contract.direct_planned_count == 2
    assert contract.reply_min_required_count == 3
    assert session.scalar(select(func.count(ContentMixObligation.id))) == 3
    assert session.scalar(select(func.count(AuditLog.id))) == 1
    assert session.get(Action, "failed-action-1").payload["relation_kind"] == "reply"


def test_recovery_excludes_any_slot_with_gateway_side_effect(session: Session) -> None:
    session.add(ExecutionAttempt(
        id="gateway-attempt",
        tenant_id=1,
        action_id="failed-action-1",
        attempt_no=1,
        status="after_call",
        gateway_call_started_at=datetime.now(timezone.utc),
    ))
    session.commit()

    snapshot = build_reply_ratio_recovery_snapshot(
        session,
        task_ids=("task-reply-recovery",),
        target_date=TARGET_DATE,
        per_task_limit=2,
    )

    ordered_slot_ids = list(session.scalars(
        select(ContentMixCycleSlot.id).order_by(ContentMixCycleSlot.slot_index),
    ))
    assert snapshot["tasks"][0]["candidate_slot_ids"] == ordered_slot_ids[1:3]


def test_recovery_rejects_mismatched_preview_hash(session: Session) -> None:
    snapshot = build_reply_ratio_recovery_snapshot(
        session,
        task_ids=("task-reply-recovery",),
        target_date=TARGET_DATE,
        per_task_limit=1,
    )

    with pytest.raises(RuntimeError, match="state hash changed"):
        apply_reply_ratio_recovery(
            session,
            snapshot=snapshot,
            expected_state_hash="0" * 64,
            actor="incident:test",
        )


def _seed_replan_cycle(session: Session) -> None:
    session.add_all([
        Tenant(id=1, name="单用户"),
        OperationTarget(id=11, tenant_id=1, tg_peer_id="-10011", title="目标群"),
        Task(
            id="task-reply-recovery",
            tenant_id=1,
            name="AI 活群",
            type="group_ai_chat",
            type_config={"messages_per_round": 5, "reply_min_per_round": 1},
        ),
        TaskDayLedger(
            id="ledger-reply-recovery",
            tenant_id=1,
            task_id="task-reply-recovery",
            timezone_snapshot="Asia/Shanghai",
            timezone_revision=1,
            obligation_local_date=TARGET_DATE,
            period_start_at=datetime(2026, 8, 2, 16, tzinfo=timezone.utc),
            deadline_at=datetime(2026, 8, 3, 16, tzinfo=timezone.utc),
            day_phase="full_day",
            planning_anchor_at=datetime(2026, 8, 2, 16, tzinfo=timezone.utc),
        ),
    ])
    session.add_all([
        TaskGroupDailyMessageSlot(
            id=f"quantity-{index}",
            tenant_id=1,
            task_id="task-reply-recovery",
            task_day_ledger_id="ledger-reply-recovery",
            target_operation_target_id=11,
            slot_kind="extra_volume",
            slot_ordinal=index,
        )
        for index in range(1, 6)
    ])
    session.commit()
    cycle = create_content_mix_cycle(session, ContentMixCycleSpec(
        tenant_id=1,
        task_id="task-reply-recovery",
        target_operation_target_id=11,
        task_day_ledger_id="ledger-reply-recovery",
        cycle_seq=1,
        config_revision=1,
        allocation_seed="bad-micro-batch",
        slots=tuple(
            ContentMixSlotSpec(f"quantity-{index}", "reply", f"reply-{index}", str(index))
            for index in range(1, 6)
        ),
        reply_min_required_count=5,
    ))
    session.flush()
    slots = list(session.scalars(
        select(ContentMixCycleSlot).where(ContentMixCycleSlot.cycle_id == cycle.id).order_by(ContentMixCycleSlot.slot_index),
    ))
    for index, slot in enumerate(slots, start=1):
        action = Action(
            id=f"failed-action-{index}",
            tenant_id=1,
            task_id="task-reply-recovery",
            task_type="group_ai_chat",
            action_type="send_message",
            status="failed",
            content_mix_cycle_slot_id=slot.id,
            content_mix_slot_attempt=1,
            primary_quantity_slot_id=f"quantity-{index}",
            payload={"relation_kind": "reply"},
        )
        session.add(action)
        session.flush()
        slot.slot_state = "replan_required"
        slot.current_action_id = action.id
    session.commit()
