from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiGroupContentAllocationPlan,
    AiGroupContentIntent,
    Task,
    TaskGroupDailyMessageSlot,
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
from ai_group_content_test_support import (
    _payload_for_slot,
    TASK_DAY, _assignments, _freeze, _freeze_questions, _confirm_remote,
    session as session,
)

pytestmark = pytest.mark.no_postgres


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
    payload = _payload_for_slot(session, frozen)
    validate_content_intent_for_gateway(session, payload)

    with pytest.raises(ValueError, match="topic_contract_revision_drift"):
        validate_content_intent_for_gateway(
            session, payload.model_copy(update={"content_task_day": "2026-09-01"})
        )
