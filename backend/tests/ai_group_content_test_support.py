from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AiGroupContentIntent,
    FulfillmentRemoteFact,
    OperationTarget,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    TaskGroupDailyTarget,
    Tenant,
    TgGroup,
)
from app.services.task_center.ai_group_content_allocation import (
    freeze_content_intents,
)
from app.services.task_center.ai_pacing import AiPacingAssignment
from app.services.task_center.payloads import SendMessagePayload


pytestmark = pytest.mark.no_postgres
TASK_DAY = date(2026, 8, 31)
NOW = datetime(2026, 8, 31, 8, tzinfo=timezone.utc)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        _seed_scope(current)
        yield current


def _seed_scope(session: Session) -> None:
    session.add(Tenant(id=1, name="allocation-tenant"))
    session.flush()
    session.add(TgGroup(id=21, tenant_id=1, tg_peer_id="-10021", title="目标群"))
    session.add(
        OperationTarget(id=31, tenant_id=1, tg_peer_id="-10021", title="目标群")
    )
    session.add(
        Task(
            id="allocation-task",
            tenant_id=1,
            name="AI 活群",
            type="group_ai_chat",
            type_config={
                "target_operation_target_id": 31,
                "topic_participation_rate": 0.30,
                "topic_directions": [{"title": "任务话题 A", "weight": 1}],
                "content_route": "general",
                "_ai_group_content_policy_revision": 5,
            },
        )
    )
    session.flush()
    _seed_calendar(session)
    session.commit()


def _seed_calendar(session: Session) -> None:
    session.add(
        TaskDayLedger(
            id="ledger-1",
            tenant_id=1,
            task_id="allocation-task",
            timezone_snapshot="Asia/Shanghai",
            timezone_revision=1,
            obligation_local_date=TASK_DAY,
            period_start_at=NOW,
            deadline_at=NOW,
            day_phase="full_day",
            planning_anchor_at=NOW,
        )
    )
    session.flush()
    session.add(
        TaskGroupDailyTarget(
            id="daily-target-1",
            tenant_id=1,
            task_id="allocation-task",
            task_day_ledger_id="ledger-1",
            group_id=21,
            target_date=TASK_DAY,
            configured_message_target=10,
            frozen_account_count=1,
            effective_message_target=10,
            daily_fulfillment_phase="active",
            scope_frozen_at=NOW,
            full_day_committed_at=NOW,
        )
    )
    session.commit()


def _assignments(
    session: Session, ordinals: list[int]
) -> tuple[list[AiPacingAssignment], list[dict]]:
    rows = []
    items = []
    for index, ordinal in enumerate(ordinals):
        owner = TaskGroupDailyMessageSlot(
            id=f"quantity-{ordinal}",
            tenant_id=1,
            task_id="allocation-task",
            task_day_ledger_id="ledger-1",
            target_operation_target_id=31,
            slot_kind="quantity",
            slot_ordinal=ordinal,
        )
        session.add(owner)
        rows.append(AiPacingAssignment(index, owner, SimpleNamespace()))
        items.append(
            {
                "slot": {
                    "slot_id": f"logical-{ordinal}",
                    "act_type": "short_react",
                    "stance": "positive",
                },
                "defer_ai_generation": True,
            }
        )
    session.flush()
    return rows, items


def _freeze(session: Session, ordinals: list[int]) -> list[dict]:
    assignments, items = _assignments(session, ordinals)
    task = session.get(Task, "allocation-task")
    return freeze_content_intents(
        session,
        task,
        daily_group_target_id="daily-target-1",
        target_operation_target_id=31,
        canonical_group_id=21,
        assignments=assignments,
        quality_items=items,
        config_revision=1,
        is_generic_warmup=False,
    )


def _freeze_questions(session: Session, ordinals: list[int]) -> list[dict]:
    assignments, items = _assignments(session, ordinals)
    for item in items:
        item["slot"]["act_type"] = "question"
        item["slot"]["stance"] = "neutral"
    task = session.get(Task, "allocation-task")
    return freeze_content_intents(
        session,
        task,
        daily_group_target_id="daily-target-1",
        target_operation_target_id=31,
        canonical_group_id=21,
        assignments=assignments,
        quality_items=items,
        config_revision=1,
        is_generic_warmup=False,
    )


def _confirm_remote(session: Session, intent: AiGroupContentIntent, index: int) -> None:
    action = Action(
        id=f"action-{index}",
        tenant_id=1,
        task_id="allocation-task",
        task_type="group_ai_chat",
        action_type="send_message",
        primary_quantity_slot_id=intent.primary_quantity_slot_id,
        status="success",
    )
    session.add(action)
    session.add(
        FulfillmentRemoteFact(
            fact_id=f"fact-{index}",
            tenant_id=1,
            task_type="group_ai_chat",
            task_id="allocation-task",
            task_day_ledger_id="ledger-1",
            obligation_type="ai_send",
            obligation_id=intent.primary_quantity_slot_id,
            action_id=action.id,
            attempt_id=f"attempt-{index}",
            mutation_kind="send_message",
            remote_mutation_key_hash=f"mutation-{index}",
            gateway_request_hash=f"gateway-{index}",
            fact_kind="remote_message_observed",
            fact_identity_hash=f"identity-{index}",
            outcome={"remote_message_id": str(index)},
            observed_at=NOW,
        )
    )


def _payload_for_slot(session: Session, slot: dict) -> SendMessagePayload:
    return SendMessagePayload(
        group_id=21,
        message_text="测试内容",
        primary_quantity_slot_id=f"quantity-{slot['normal_text_ordinal']}",
        allocation_plan_id=slot["allocation_plan_id"],
        content_intent_id=slot["content_intent_id"],
        content_intent_config_revision=slot["content_intent_config_revision"],
        content_intent_config_snapshot_hash=slot["content_intent_config_snapshot_hash"],
        content_intent_task_lifecycle_epoch=slot["content_intent_task_lifecycle_epoch"],
        content_intent_target_reference_revision=slot["content_intent_target_reference_revision"],
        content_contract_revision=slot["content_contract_revision"],
        task_config_revision=session.get(Task, "allocation-task").config_revision,
        target_operation_target_id=31,
        target_reference_revision=1,
        normal_text_ordinal=slot["normal_text_ordinal"],
        relation_kind=slot["relation_kind"],
        act_type=slot["act_type"],
        content_intent_stance=slot["stance"],
        topic_rate_bps=slot["topic_rate_bps"],
        topic_budget_eligible=slot["topic_budget_eligible"],
        topic_mode=slot["topic_mode"],
        topic_capacity_reservation_id=slot["topic_capacity_reservation_id"],
        topic_direction=slot.get("topic_direction", {}),
        teacher_target=slot.get("teacher_target", {}),
        surface_scope_key=slot["surface_scope_key"],
        topic_ratio_scope_key=slot["topic_ratio_scope_key"],
        content_task_day=slot["task_day"],
        route_family=slot["route_family"],
        daily_vocabulary_theme_id=slot["daily_vocabulary_theme_id"],
        daily_vocabulary_theme_version=slot["daily_vocabulary_theme_version"],
        daily_vocabulary_theme_effective_state=slot["daily_vocabulary_theme_effective_state"],
        vocabulary_catalog_version=slot["vocabulary_catalog_version"],
        vocabulary_sample_ids=slot["vocabulary_sample_ids"],
        vocabulary_surface_terms=slot["vocabulary_surface_terms"],
        vocabulary_normalized_term_ids=slot["vocabulary_normalized_term_ids"],
        vocabulary_candidate_count=slot["vocabulary_candidate_count"],
        vocabulary_reservation_id=slot["vocabulary_reservation_id"],
    )
