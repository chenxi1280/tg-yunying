from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ContentMixCycle,
    ContentMixCycleSlot,
    ContentMixObligation,
    FulfillmentObligationProjection,
    TaskGroupDailyMessageSlot,
)
from app.services.task_center.generation_shortfall_projection import (
    project_generation_shortfall,
)


pytestmark = pytest.mark.no_postgres


def test_group_generation_shortfall_closes_quantity_and_obligation_owners() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        action = _seed_group_owners(session)

        project_generation_shortfall(
            session,
            action,
            reason_code="quality_wait_deadline",
        )
        session.flush()

        projection = session.get(FulfillmentObligationProjection, "projection-1")
        slot = session.get(ContentMixCycleSlot, "mix-slot-1")
        quantity = session.get(TaskGroupDailyMessageSlot, "quantity-slot-1")
        obligation = session.get(ContentMixObligation, "mix-obligation-1")
        assert projection.state == "terminal_shortfall"
        assert projection.version == 2
        assert slot.slot_state == "terminal"
        assert slot.terminal_reason == "quality_wait_deadline"
        assert quantity.state == "terminal"
        assert obligation.status == "shortfall"
        assert obligation.shortfall_count == obligation.required_count


def test_generation_shortfall_rejects_foreign_projection_owner() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        action = _seed_group_owners(session)
        projection = session.get(FulfillmentObligationProjection, "projection-1")
        projection.active_action_id = "another-action"
        session.flush()

        with pytest.raises(
            RuntimeError,
            match="generation_shortfall_obligation_projection_conflict",
        ):
            project_generation_shortfall(
                session,
                action,
                reason_code="quality_wait_deadline",
            )


def _seed_group_owners(session: Session) -> Action:
    action = _action()
    session.add_all([
        action,
        _projection(action),
        _quantity_slot(),
        _cycle(),
        _cycle_slot(action),
        _content_obligation(action),
    ])
    session.flush()
    return action


def _action() -> Action:
    return Action(
        id="action-1",
        tenant_id=1,
        task_id="task-1",
        task_type="group_ai_chat",
        action_type="send_message",
        status="failed",
        scheduled_at=datetime(2026, 8, 19, 12, 0),
        obligation_type="quantity_slot",
        obligation_id="quantity-slot-1",
        content_mix_cycle_slot_id="mix-slot-1",
    )


def _projection(action: Action) -> FulfillmentObligationProjection:
    return FulfillmentObligationProjection(
        id="projection-1",
        tenant_id=1,
        task_id="task-1",
        obligation_type="quantity_slot",
        obligation_id="quantity-slot-1",
        work_lane="ai_generation",
        active_action_id=action.id,
        state="open",
        version=1,
    )


def _quantity_slot() -> TaskGroupDailyMessageSlot:
    return TaskGroupDailyMessageSlot(
        id="quantity-slot-1",
        tenant_id=1,
        task_id="task-1",
        task_day_ledger_id="ledger-1",
        target_operation_target_id=1,
        slot_kind="coverage",
        slot_ordinal=1,
        state="reserved",
    )


def _cycle() -> ContentMixCycle:
    return ContentMixCycle(
        id="mix-cycle-1",
        tenant_id=1,
        task_id="task-1",
        target_operation_target_id=1,
        task_day_ledger_id="ledger-1",
        cycle_seq=1,
        config_revision=1,
        scope_total_slots=2,
        allocation_seed="seed",
        allocation_closed_at=datetime(2026, 8, 19, 12, 0),
    )


def _cycle_slot(action: Action) -> ContentMixCycleSlot:
    return ContentMixCycleSlot(
        id="mix-slot-1",
        tenant_id=1,
        cycle_id="mix-cycle-1",
        slot_index=0,
        primary_quantity_slot_id="quantity-slot-1",
        relation_kind="direct",
        current_action_id=action.id,
        slot_state="pending",
    )


def _content_obligation(action: Action) -> ContentMixObligation:
    return ContentMixObligation(
        id="mix-obligation-1",
        tenant_id=1,
        content_mix_contract_id="mix-contract-1",
        content_mix_scope_key="scope-1",
        obligation_source="policy",
        obligation_kind="normal_text_emoji",
        obligation_ordinal=1,
        assigned_cycle_slot_id="mix-slot-1",
        assigned_action_id=action.id,
        required_count=1,
        planned_count=1,
        status="pending",
    )
