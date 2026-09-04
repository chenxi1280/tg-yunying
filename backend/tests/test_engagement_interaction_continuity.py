from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ContextTurn,
    ConversationEvent,
    ConversationTurnClaim,
    InteractionContinuityCapacityPlan,
    InteractionOpportunity,
    ManagedPresencePolicyRevision,
    OperationTarget,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    Tenant,
    TgGroup,
)
from app.services.task_center.engagement_interaction_continuity import (
    ensure_interaction_continuity_capacity,
)
from app.services.task_center.engagement_conversation import (
    settle_conversation_turn_claim,
)


pytestmark = pytest.mark.no_postgres
DAY_START = datetime(2026, 9, 3, 16, tzinfo=timezone.utc)


def test_continuity_capacity_is_claim_owned_and_never_quantity_credit() -> None:
    with _session() as session:
        task = session.get(Task, "continuity-task")
        ledger = session.get(TaskDayLedger, "continuity-day")
        group = session.get(TgGroup, 21)
        targets = [_reply_target("claim-1", 101), _reply_target("claim-2", 102)]

        first = ensure_interaction_continuity_capacity(
            session, task, ledger, group,
            operation_target_id=31, reply_targets=targets,
        )
        second = ensure_interaction_continuity_capacity(
            session, task, ledger, group,
            operation_target_id=31, reply_targets=targets,
        )

        slots = list(session.scalars(select(TaskGroupDailyMessageSlot)))
        assert len(first.admitted_targets) == 1
        assert first.plan.rejected_by_capacity_count == 1
        assert second.plan.id == first.plan.id
        assert len(slots) == 1
        assert slots[0].slot_kind == "interaction_continuity"
        assert slots[0].continuity_claim_id == "claim-1"
        assert slots[0].quantity_credit_eligible is False
        assert task.stats["interaction_continuity"]["quantity_credit"] == 0


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="租户"))
    session.add(TgGroup(id=21, tenant_id=1, tg_peer_id="-10021", title="群"))
    session.add(OperationTarget(
        id=31, tenant_id=1, target_type="group", tg_peer_id="-10021",
        title="群", auth_status="已授权运营", can_send=True,
    ))
    session.add(ManagedPresencePolicyRevision(
        tenant_id=1, max_consecutive_system_turns=1,
        absolute_daily_authored_cap=20,
        managed_to_external_ratio_bps=10000, bootstrap_allowance=0,
    ))
    session.add(Task(
        id="continuity-task", tenant_id=1, name="活群",
        type="group_ai_chat", status="running", stats={},
        type_config={"engagement_contract_version": "unified_engagement_v1"},
    ))
    session.flush()
    session.add(TaskDayLedger(
        id="continuity-day", tenant_id=1, task_id="continuity-task",
        timezone_snapshot="Asia/Shanghai", timezone_revision=1,
        obligation_local_date=date(2026, 9, 4),
        period_start_at=DAY_START, deadline_at=DAY_START + timedelta(days=1),
        day_phase="full_day", planning_anchor_at=DAY_START,
    ))
    _add_claim(session, 1)
    _add_claim(session, 2)
    session.commit()
    return session


def _add_claim(session: Session, index: int) -> None:
    sent_at = DAY_START + timedelta(minutes=index)
    event = ConversationEvent(
        id=f"event-{index}", tenant_id=1, surface="group_ai_chat",
        canonical_peer_id="-10021", target_group_id=21,
        remote_message_id=str(100 + index), author_class="external_human",
        author_peer_id=f"human-{index}", author_name="真人",
        content=f"问题{index}", content_hash=f"hash-{index}",
        source_context_message_id=100 + index, sent_at=sent_at,
    )
    turn = ContextTurn(
        id=f"turn-{index}", tenant_id=1, surface="group_ai_chat",
        canonical_peer_id="-10021", target_group_id=21,
        turn_family_key=f"family-{index}", anchor_event_id=event.id,
        event_ids=[event.id], state="closed", first_event_at=sent_at,
        last_event_at=sent_at, closed_at=sent_at,
    )
    opportunity = InteractionOpportunity(
        id=f"opportunity-{index}", tenant_id=1, task_id="continuity-task",
        task_lifecycle_epoch=1, context_turn_id=turn.id,
        anchor_event_id=event.id, state="admitted",
        natural_not_before_at=sent_at,
        freshness_deadline_at=sent_at + timedelta(minutes=2),
    )
    claim = ConversationTurnClaim(
        id=f"claim-{index}", tenant_id=1, context_turn_id=turn.id,
        interaction_opportunity_id=opportunity.id,
        task_id="continuity-task", task_lifecycle_epoch=1,
    )
    session.add_all([event, turn, opportunity, claim])


def _reply_target(claim_id: str, message_id: int) -> dict:
    return {
        "conversation_turn_claim_id": claim_id,
        "message_id": message_id,
    }


def test_capacity_plan_is_persisted_as_auditable_business_fact() -> None:
    with _session() as session:
        task = session.get(Task, "continuity-task")
        ledger = session.get(TaskDayLedger, "continuity-day")
        group = session.get(TgGroup, 21)
        decision = ensure_interaction_continuity_capacity(
            session, task, ledger, group,
            operation_target_id=31,
            reply_targets=[_reply_target("claim-1", 101), _reply_target("claim-2", 102)],
        )

        persisted = session.get(InteractionContinuityCapacityPlan, decision.plan.id)
        assert persisted.observed_eligible_demand == 2
        assert persisted.admitted_count == 1
        assert persisted.rejected_by_capacity_count == 1
        assert persisted.decision == "capacity_exhausted"


def test_continuity_settlement_updates_service_facts_without_quantity_credit() -> None:
    with _session() as session:
        task = session.get(Task, "continuity-task")
        ledger = session.get(TaskDayLedger, "continuity-day")
        group = session.get(TgGroup, 21)
        decision = ensure_interaction_continuity_capacity(
            session, task, ledger, group,
            operation_target_id=31,
            reply_targets=[_reply_target("claim-1", 101)],
        )
        slot = session.scalar(select(TaskGroupDailyMessageSlot))
        action = Action(
            id="continuity-action", tenant_id=1, task_id=task.id,
            task_type="group_ai_chat", action_type="send_message",
            status="success", primary_quantity_slot_id=slot.id,
            payload={"conversation_turn_claim_id": "claim-1"},
        )
        session.add(action)
        claim = session.get(ConversationTurnClaim, "claim-1")
        claim.action_id = action.id
        claim.state = "bound"
        session.flush()

        settle_conversation_turn_claim(session, action, outcome="served")

        assert decision.plan.served_count == 1
        assert decision.plan.remaining_capacity == 0
        assert decision.plan.version == 2
        assert task.stats["interaction_continuity"]["served_count"] == 1
