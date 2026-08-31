from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AiGroupContentAllocationPlan,
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
    validate_content_intent_for_gateway,
)
from app.services.task_center.ai_group_content_projection import (
    plan_intent_remote_states,
)
from app.services.task_center.ai_group_content_history import recent_content_history
from app.services.task_center.ai_group_content_intent_support import (
    GenericWarmupQuestionWait,
)
from app.services.task_center.ai_pacing import AiPacingAssignment
from app.services.task_center.ai_group_content_read_model import (
    ai_group_content_allocation_summary,
)
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
def test_plan_freezes_one_immutable_intent_per_quantity_slot(session: Session) -> None:
    first = _freeze(session, [1])[0]["slot"]
    session.flush()
    task = session.get(Task, "allocation-task")
    owner = session.get(TaskGroupDailyMessageSlot, "quantity-1")
    repeated = freeze_content_intents(
        session,
        task,
        daily_group_target_id="daily-target-1",
        target_operation_target_id=31,
        canonical_group_id=21,
        assignments=[AiPacingAssignment(0, owner, SimpleNamespace())],
        quality_items=[{"slot": {"slot_id": "retry", "act_type": "question"}}],
        config_revision=2,
        is_generic_warmup=True,
    )[0]["slot"]

    assert repeated["content_intent_id"] == first["content_intent_id"]
    assert repeated["normal_text_ordinal"] == 1
    assert repeated["topic_mode"] == first["topic_mode"]
    assert repeated["vocabulary_sample_ids"] == first["vocabulary_sample_ids"]
    assert session.scalar(select(AiGroupContentAllocationPlan)).normal_text_cursor == 1


def test_remote_fact_capacity_allows_first_topic_only_after_three_confirmed_normals(
    session: Session,
) -> None:
    first_three = _freeze(session, [1, 2, 3])
    assert [item["slot"]["topic_mode"] for item in first_three] == [
        "group_free_chat",
        "group_free_chat",
        "group_free_chat",
    ]
    intents = list(
        session.scalars(
            select(AiGroupContentIntent).order_by(
                AiGroupContentIntent.normal_text_ordinal
            )
        )
    )
    for index, intent in enumerate(intents, 1):
        _confirm_remote(session, intent, index)
    session.flush()

    fourth = _freeze(session, [4])[0]["slot"]
    assert fourth["normal_text_ordinal"] == 4
    assert fourth["topic_budget_eligible"] is True
    assert fourth["topic_mode"] == "configured_topic"
    assert fourth["topic_direction"]["title"] == "任务话题 A"
    assert fourth["topic_capacity_reservation_id"]

    session.add(
        Action(
            id="action-4",
            tenant_id=1,
            task_id="allocation-task",
            task_type="group_ai_chat",
            action_type="send_message",
            primary_quantity_slot_id="quantity-4",
            status="pending",
        )
    )
    session.flush()
    payload = _payload_for_slot(session, fourth)
    validate_content_intent_for_gateway(session, payload)
    assert payload.task_config_revision == 1
    assert payload.content_intent_config_revision == 5

    summary = ai_group_content_allocation_summary(
        session, session.get(Task, "allocation-task")
    )
    assert summary["planned_topic_count"] == 1
    assert summary["remote_normal_count"] == 3
    assert summary["remote_topic_count"] == 0
    assert summary["active_topic_reservation_count"] == 1
    assert summary["daily_vocabulary_theme_id"] >= 0

    with pytest.raises(ValueError, match="topic_capacity_contract_invalid"):
        validate_content_intent_for_gateway(
            session,
            payload.model_copy(update={"topic_capacity_reservation_id": "tampered"}),
        )

    tampered_values = {
        "normal_text_ordinal": 99,
        "topic_budget_eligible": False,
        "relation_kind": "reply",
        "act_type": "question",
        "content_intent_stance": "tampered",
        "daily_vocabulary_theme_effective_state": "tampered",
        "vocabulary_catalog_version": "tampered",
        "vocabulary_normalized_term_ids": ["tampered"],
    }
    for field, value in tampered_values.items():
        with pytest.raises(ValueError):
            validate_content_intent_for_gateway(
                session,
                payload.model_copy(update={field: value}),
            )


def test_actionless_intents_reserve_topic_capacity_and_normal_denominator(
    session: Session,
) -> None:
    frozen = _freeze(session, [1, 2, 3, 4, 5, 6, 7])

    assert [item["slot"]["topic_mode"] for item in frozen] == [
        "group_free_chat",
        "group_free_chat",
        "group_free_chat",
        "configured_topic",
        "group_free_chat",
        "group_free_chat",
        "configured_topic",
    ]
    summary = ai_group_content_allocation_summary(
        session, session.get(Task, "allocation-task")
    )
    assert summary["active_topic_reservation_count"] == 2
    assert summary["planned_topic_ratio"] == 0.2857


def test_gateway_does_not_use_unsent_normal_reservations_as_remote_denominator(
    session: Session,
) -> None:
    frozen = _freeze(session, [1, 2, 3, 4])
    topic_slot = frozen[3]["slot"]
    session.add(
        Action(
            id="action-topic-prefix",
            tenant_id=1,
            task_id="allocation-task",
            task_type="group_ai_chat",
            action_type="send_message",
            primary_quantity_slot_id="quantity-4",
            status="pending",
        )
    )
    session.flush()

    with pytest.raises(ValueError, match="topic_capacity_contract_invalid"):
        validate_content_intent_for_gateway(
            session,
            _payload_for_slot(session, topic_slot),
            remote_boundary=True,
        )


def test_failed_action_keeps_intent_active_while_quantity_obligation_is_open(
    session: Session,
) -> None:
    frozen = _freeze(session, [1])[0]["slot"]
    session.add(
        Action(
            id="action-failed-open-owner",
            tenant_id=1,
            task_id="allocation-task",
            task_type="group_ai_chat",
            action_type="send_message",
            primary_quantity_slot_id="quantity-1",
            status="failed",
        )
    )
    session.flush()
    plan_id = frozen["allocation_plan_id"]

    assert plan_intent_remote_states(session, plan_id)[0][1] == "active"
    session.get(TaskGroupDailyMessageSlot, "quantity-1").state = "terminal"
    session.flush()
    assert plan_intent_remote_states(session, plan_id)[0][1] == "released"


def test_confirmed_history_uses_actual_output_not_unused_reservation(
    session: Session,
) -> None:
    frozen = _freeze(session, [1])[0]["slot"]
    intent = session.get(AiGroupContentIntent, frozen["content_intent_id"])
    assert intent.vocabulary_sample_ids
    _confirm_remote(session, intent, 1)
    session.flush()

    history = recent_content_history(
        session,
        frozen["surface_scope_key"],
        include_route_family=True,
        limit=10,
    )

    assert history[0].state == "confirmed"
    assert history[0].sample_ids == ()
    assert history[0].term_ids == ()


def test_new_intent_rejects_missing_stance(session: Session) -> None:
    assignments, items = _assignments(session, [1])
    items[0]["slot"].pop("stance")

    with pytest.raises(ValueError, match="content_intent_stance_required"):
        freeze_content_intents(
            session,
            session.get(Task, "allocation-task"),
            daily_group_target_id="daily-target-1",
            target_operation_target_id=31,
            canonical_group_id=21,
            assignments=assignments,
            quality_items=items,
            config_revision=1,
            is_generic_warmup=False,
        )


def test_unknown_topic_is_included_in_remote_ratio_denominator(
    session: Session,
) -> None:
    _freeze(session, [1, 2, 3])
    intents = list(
        session.scalars(
            select(AiGroupContentIntent).order_by(
                AiGroupContentIntent.normal_text_ordinal
            )
        )
    )
    for index, intent in enumerate(intents, 1):
        _confirm_remote(session, intent, index)
    fourth = _freeze(session, [4])[0]["slot"]
    session.add(
        Action(
            id="action-unknown",
            tenant_id=1,
            task_id="allocation-task",
            task_type="group_ai_chat",
            action_type="send_message",
            primary_quantity_slot_id="quantity-4",
            status="unknown_after_send",
        )
    )
    session.flush()

    summary = ai_group_content_allocation_summary(
        session, session.get(Task, "allocation-task")
    )

    assert fourth["topic_mode"] == "configured_topic"
    assert summary["remote_normal_count"] == 3
    assert summary["unknown_topic_hold_count"] == 1
    assert summary["remote_topic_ratio"] == 0.25


def test_read_model_aggregates_all_route_plans_for_the_same_task_day(
    session: Session,
) -> None:
    _freeze(session, [1])
    session.add(
        AiGroupContentAllocationPlan(
            id="plan-adult-route",
            tenant_id=1,
            task_id="allocation-task",
            task_day_ledger_id="ledger-1",
            target_operation_target_id=31,
            task_day=TASK_DAY,
            route_family="adult",
            surface_scope_key="tenant:1:group:21:route:adult",
            config_revision=5,
            config_snapshot_hash="adult-snapshot",
            topic_rate_bps=3000,
            normal_text_cursor=1,
            question_count=0,
            daily_vocabulary_theme_id=3,
            daily_vocabulary_theme_version="v1",
        )
    )
    session.add(
        TaskGroupDailyMessageSlot(
            id="quantity-adult-1",
            tenant_id=1,
            task_id="allocation-task",
            task_day_ledger_id="ledger-1",
            target_operation_target_id=31,
            slot_kind="quantity",
            slot_ordinal=20,
        )
    )
    session.add(
        AiGroupContentIntent(
            id="intent-adult-1",
            tenant_id=1,
            task_id="allocation-task",
            allocation_plan_id="plan-adult-route",
            primary_quantity_slot_id="quantity-adult-1",
            normal_text_ordinal=1,
            config_revision=5,
            config_snapshot_hash="adult-snapshot",
            task_lifecycle_epoch=1,
            target_reference_revision=1,
            relation_kind="direct",
            act_type="short_react",
            stance="positive",
            topic_budget_eligible=True,
            topic_mode="configured_topic",
            topic_direction_snapshot={"title": "任务话题 A"},
            teacher_target_snapshot={},
            topic_capacity_reservation_id="adult-topic-reservation",
            daily_vocabulary_theme_id=3,
            daily_vocabulary_theme_effective_state="active",
            vocabulary_catalog_version="v1.2.0",
            vocabulary_sample_ids=[],
            vocabulary_surface_terms=[],
            vocabulary_normalized_term_ids=[],
            vocabulary_candidate_count=0,
            vocabulary_reservation_id="",
        )
    )
    session.add(
        Action(
            id="action-adult-unknown",
            tenant_id=1,
            task_id="allocation-task",
            task_type="group_ai_chat",
            action_type="send_message",
            primary_quantity_slot_id="quantity-adult-1",
            status="unknown_after_send",
        )
    )
    session.flush()

    summary = ai_group_content_allocation_summary(
        session, session.get(Task, "allocation-task")
    )

    assert set(summary["allocation_plan_ids"]) == {
        "plan-adult-route",
        session.scalar(
            select(AiGroupContentAllocationPlan.id).where(
                AiGroupContentAllocationPlan.route_family == "general"
            )
        ),
    }
    assert summary["route_families"] == ["adult", "general"]
    assert summary["unknown_topic_hold_count"] == 1
    assert summary["remote_topic_capacity_numerator"] == 1
    assert summary["remote_topic_capacity_denominator"] == 1
    assert summary["remote_topic_ratio"] == 1.0


def test_allocation_owner_freezes_question_mix_before_intent_creation(
    session: Session,
) -> None:
    frozen = _freeze_questions(session, [1, 2, 3, 4, 5, 6])
    acts = [item["slot"]["act_type"] for item in frozen]

    assert acts == [
        "question",
        "question",
        "short_react",
        "question",
        "question",
        "short_react",
    ]
    assert session.scalar(select(AiGroupContentAllocationPlan)).question_count == 4


def test_generic_warmup_waits_instead_of_changing_required_question(
    session: Session,
) -> None:
    first_two = _freeze_questions(session, [1, 2])
    intents = list(
        session.scalars(
            select(AiGroupContentIntent).order_by(
                AiGroupContentIntent.normal_text_ordinal
            )
        )
    )
    for index, intent in enumerate(intents, 1):
        _confirm_remote(session, intent, index)
    session.flush()
    assignments, items = _assignments(session, [3])
    items[0]["slot"]["act_type"] = "question"

    with pytest.raises(GenericWarmupQuestionWait, match="question_mix_wait"):
        freeze_content_intents(
            session,
            session.get(Task, "allocation-task"),
            daily_group_target_id="daily-target-1",
            target_operation_target_id=31,
            canonical_group_id=21,
            assignments=assignments,
            quality_items=items,
            config_revision=1,
            is_generic_warmup=True,
        )

    assert [item["slot"]["act_type"] for item in first_two] == ["question", "question"]
    assert session.scalar(select(AiGroupContentAllocationPlan)).normal_text_cursor == 2


def test_weighted_topic_direction_prefers_underused_high_weight_topic(
    session: Session,
) -> None:
    task = session.get(Task, "allocation-task")
    task.type_config = {
        **task.type_config,
        "topic_directions": [
            {"title": "低权重", "weight": 1},
            {"title": "高权重", "weight": 10},
        ],
    }
    first_three = _freeze(session, [1, 2, 3])
    intents = list(
        session.scalars(
            select(AiGroupContentIntent).order_by(
                AiGroupContentIntent.normal_text_ordinal
            )
        )
    )
    for index, intent in enumerate(intents, 1):
        _confirm_remote(session, intent, index)
    session.flush()

    fourth = _freeze(session, [4])[0]["slot"]

    assert first_three[0]["slot"]["topic_mode"] == "group_free_chat"
    assert fourth["topic_mode"] == "configured_topic"
    assert fourth["topic_direction"]["title"] == "高权重"


def test_gateway_rejects_content_scope_drift(session: Session) -> None:
    _freeze(session, [1, 2, 3])
    intents = list(
        session.scalars(
            select(AiGroupContentIntent).order_by(
                AiGroupContentIntent.normal_text_ordinal
            )
        )
    )
    for index, intent in enumerate(intents, 1):
        _confirm_remote(session, intent, index)
    session.flush()
    frozen = _freeze(session, [4])[0]["slot"]
    session.add(
        Action(
            id="action-scope",
            tenant_id=1,
            task_id="allocation-task",
            task_type="group_ai_chat",
            action_type="send_message",
            primary_quantity_slot_id="quantity-4",
            status="pending",
        )
    )
    session.flush()
    payload = SendMessagePayload(
        group_id=21,
        message_text="测试内容",
        target_operation_target_id=31,
        target_reference_revision=1,
        task_config_revision=frozen["content_intent_config_revision"],
        primary_quantity_slot_id="quantity-4",
        allocation_plan_id=frozen["allocation_plan_id"],
        content_intent_id=frozen["content_intent_id"],
        content_intent_config_revision=frozen["content_intent_config_revision"],
        content_intent_config_snapshot_hash=frozen[
            "content_intent_config_snapshot_hash"
        ],
        content_intent_task_lifecycle_epoch=frozen[
            "content_intent_task_lifecycle_epoch"
        ],
        content_intent_target_reference_revision=frozen[
            "content_intent_target_reference_revision"
        ],
        content_contract_revision=frozen["content_contract_revision"],
        topic_rate_bps=frozen["topic_rate_bps"],
        topic_mode=frozen["topic_mode"],
        topic_capacity_reservation_id=frozen["topic_capacity_reservation_id"],
        topic_direction=frozen["topic_direction"],
        surface_scope_key=frozen["surface_scope_key"],
        topic_ratio_scope_key=frozen["topic_ratio_scope_key"],
        content_task_day=frozen["task_day"],
        route_family=frozen["route_family"],
        daily_vocabulary_theme_id=frozen["daily_vocabulary_theme_id"],
        daily_vocabulary_theme_version=frozen["daily_vocabulary_theme_version"],
        vocabulary_sample_ids=frozen["vocabulary_sample_ids"],
        vocabulary_surface_terms=frozen["vocabulary_surface_terms"],
        vocabulary_candidate_count=frozen["vocabulary_candidate_count"],
        vocabulary_reservation_id=frozen["vocabulary_reservation_id"],
    )

    with pytest.raises(ValueError, match="topic_contract_revision_drift"):
        validate_content_intent_for_gateway(
            session, payload.model_copy(update={"content_task_day": "2026-09-01"})
        )
