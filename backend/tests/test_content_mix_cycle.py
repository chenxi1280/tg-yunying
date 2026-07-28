from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    ContentMixContract,
    ContentMixCycle,
    ContentMixCycleSlot,
    ContentMixObligation,
    OperationTarget,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    Tenant,
)
from app.services.task_center.content_mix_cycles import (
    ContentMixCycleSpec,
    ContentMixSlotSpec,
    create_content_mix_cycle,
    mark_cycle_slot_materialized,
    reconcile_content_mix_cycle,
)


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="单用户"))
        current.add(
            OperationTarget(
                id=11,
                tenant_id=1,
                tg_peer_id="-10011",
                title="目标群",
            )
        )
        task = Task(
            id="task-cycle",
            tenant_id=1,
            name="AI 活群",
            type="group_ai_chat",
        )
        ledger = TaskDayLedger(
            id="ledger-cycle",
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
        slots = [
            TaskGroupDailyMessageSlot(
                id=f"quantity-{index}",
                tenant_id=1,
                task_id=task.id,
                task_day_ledger_id=ledger.id,
                target_operation_target_id=11,
                slot_kind="extra_volume",
                slot_ordinal=index,
            )
            for index in range(1, 4)
        ]
        current.add_all([task, ledger, *slots])
        current.commit()
        yield current


def _spec() -> ContentMixCycleSpec:
    return ContentMixCycleSpec(
        tenant_id=1,
        task_id="task-cycle",
        target_operation_target_id=11,
        task_day_ledger_id="ledger-cycle",
        cycle_seq=1,
        config_revision=3,
        allocation_seed="stable-seed",
        slots=(
            ContentMixSlotSpec("quantity-1", "reply", "reply-1", "501"),
            ContentMixSlotSpec("quantity-2", "direct"),
            ContentMixSlotSpec("quantity-3", "direct"),
        ),
        reply_min_required_count=1,
        image_required_count=1,
        image_max_count=1,
    )


def test_cycle_contract_slots_and_policy_obligations_are_atomic(session: Session) -> None:
    cycle = create_content_mix_cycle(session, _spec())
    session.commit()

    contract = session.scalar(
        select(ContentMixContract).where(
            ContentMixContract.content_mix_scope_key
            == f"ai:task-cycle:11:{cycle.id}:3"
        )
    )
    slots = session.scalars(
        select(ContentMixCycleSlot)
        .where(ContentMixCycleSlot.cycle_id == cycle.id)
        .order_by(ContentMixCycleSlot.slot_index)
    ).all()
    obligations = session.scalars(
        select(ContentMixObligation).where(
            ContentMixObligation.content_mix_contract_id == contract.id
        )
    ).all()

    assert cycle.scope_total_slots == 3
    assert contract.reply_planned_count == 1
    assert contract.direct_planned_count == 2
    assert [slot.primary_quantity_slot_id for slot in slots] == [
        "quantity-1",
        "quantity-2",
        "quantity-3",
    ]
    assert {(item.obligation_kind, item.required_count) for item in obligations} == {
        ("reply", 1),
        ("image", 1),
    }


def test_invalid_cycle_rolls_back_without_half_contract(session: Session) -> None:
    invalid = _spec()
    invalid = ContentMixCycleSpec(
        **{
            **invalid.__dict__,
            "reply_min_required_count": 4,
        }
    )

    with pytest.raises(ValueError, match="content_mix_policy_invalid"):
        create_content_mix_cycle(session, invalid)

    assert session.scalar(select(func.count(ContentMixCycle.id))) == 0
    assert session.scalar(select(func.count(ContentMixContract.id))) == 0
    assert session.scalar(select(func.count(ContentMixCycleSlot.id))) == 0


def test_materialization_is_monotonic_and_unknown_prevents_settlement(session: Session) -> None:
    cycle = create_content_mix_cycle(session, _spec())
    session.commit()
    slots = session.scalars(
        select(ContentMixCycleSlot)
        .where(ContentMixCycleSlot.cycle_id == cycle.id)
        .order_by(ContentMixCycleSlot.slot_index)
    ).all()

    mark_cycle_slot_materialized(session, slots[0], action_id=None)
    mark_cycle_slot_materialized(session, slots[1], action_id=None)
    session.commit()
    assert cycle.materialization_status == "partial"
    assert cycle.materialized_slot_count == 2

    slots[0].slot_state = "confirmed"
    slots[1].slot_state = "terminal"
    slots[2].slot_state = "unknown"
    observed_at = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    assert reconcile_content_mix_cycle(
        session,
        cycle,
        observed_at=observed_at,
    ) is False
    assert cycle.settlement_status == "open"

    slots[2].slot_state = "terminal"
    for obligation in session.scalars(
        select(ContentMixObligation).where(
            ContentMixObligation.content_mix_contract_id.in_(
                select(ContentMixContract.id).where(
                    ContentMixContract.content_mix_scope_key
                    == f"ai:task-cycle:11:{cycle.id}:3"
                )
            )
        )
    ):
        obligation.status = "shortfall"
        obligation.shortfall_count = obligation.required_count

    assert reconcile_content_mix_cycle(
        session,
        cycle,
        observed_at=observed_at,
    ) is True
    assert cycle.settlement_status == "settled"
    assert cycle.settlement_outcome == "shortfall"
