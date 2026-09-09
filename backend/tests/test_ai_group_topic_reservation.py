from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiGroupContentIntent,
    Task,
)
from app.services.task_center.ai_group_content_allocation import (
    validate_content_intent_for_gateway,
)
from app.services.task_center.ai_group_content_read_model import (
    ai_group_content_allocation_summary,
)
from ai_group_content_test_support import (
    _freeze, _confirm_remote,
    _payload_for_slot, session as session,
)

pytestmark = pytest.mark.no_postgres


@pytest.fixture
def first_topic(session):
    _freeze(session, [1, 2, 3])
    intents = list(session.scalars(select(AiGroupContentIntent).order_by(
        AiGroupContentIntent.normal_text_ordinal)))
    for index, intent in enumerate(intents, 1):
        _confirm_remote(session, intent, index)
    fourth = _freeze(session, [4])[0]["slot"]
    session.add(Action(id="action-4", tenant_id=1, task_id="allocation-task",
        task_type="group_ai_chat", action_type="send_message",
        primary_quantity_slot_id="quantity-4", status="pending"))
    session.flush()
    return fourth, _payload_for_slot(session, fourth)


def test_first_topic_has_remote_capacity_after_three_confirmed_normals(session, first_topic):
    fourth, payload = first_topic
    assert fourth["normal_text_ordinal"] == 4
    assert fourth["topic_budget_eligible"] is True
    assert fourth["topic_mode"] == "configured_topic"
    assert fourth["topic_direction"]["title"] == "任务话题 A"
    assert fourth["topic_capacity_reservation_id"]
    validate_content_intent_for_gateway(session, payload)
    validate_content_intent_for_gateway(session, payload, remote_boundary=True)
    assert payload.task_config_revision == 1
    assert payload.content_intent_config_revision == 5
    summary = ai_group_content_allocation_summary(session, session.get(Task, "allocation-task"))
    assert summary["planned_topic_count"] == 1
    assert summary["remote_normal_count"] == 3
    assert summary["remote_topic_count"] == 0
    assert summary["active_topic_reservation_count"] == 1
    assert summary["daily_vocabulary_theme_id"] >= 0


@pytest.mark.parametrize("field,value", [
    ("topic_capacity_reservation_id", "tampered"),
    ("normal_text_ordinal", 99), ("topic_budget_eligible", False),
    ("relation_kind", "reply"), ("act_type", "question"),
    ("content_intent_stance", "tampered"),
    ("daily_vocabulary_theme_effective_state", "tampered"),
    ("vocabulary_catalog_version", "tampered"),
    ("vocabulary_normalized_term_ids", ["tampered"]),
])
def test_gateway_rejects_tampered_topic_contract(session, first_topic, *, field, value):
    _, payload = first_topic
    with pytest.raises(ValueError):
        validate_content_intent_for_gateway(session, payload.model_copy(update={field: value}))


def test_actionless_intents_cannot_supply_remote_topic_capacity(
    session: Session,
) -> None:
    frozen = _freeze(session, [1, 2, 3, 4, 5, 6, 7])

    assert [item["slot"]["topic_mode"] for item in frozen] == [
        "group_free_chat",
        "group_free_chat",
        "group_free_chat",
        "group_free_chat",
        "group_free_chat",
        "group_free_chat",
        "group_free_chat",
    ]
    summary = ai_group_content_allocation_summary(
        session, session.get(Task, "allocation-task")
    )
    assert summary["active_topic_reservation_count"] == 0
    assert summary["planned_topic_ratio"] == 0.0


def test_gateway_does_not_use_unsent_normal_reservations_as_remote_denominator(
    session: Session,
) -> None:
    frozen = _freeze(session, [1, 2, 3, 4])
    # Preserve an old overallocated intent as a historical fixture.
    topic_slot = frozen[3]["slot"]
    intent = session.get(AiGroupContentIntent, topic_slot["content_intent_id"])
    intent.topic_mode = "configured_topic"
    intent.topic_capacity_reservation_id = "legacy-reservation"
    intent.topic_direction_snapshot = {"title": "任务话题 A", "weight": 1}
    topic_slot = {**topic_slot, "topic_mode": intent.topic_mode,
        "topic_capacity_reservation_id": intent.topic_capacity_reservation_id,
        "topic_direction": intent.topic_direction_snapshot}
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


@pytest.mark.parametrize("prior_topic_status", ["pending", "unknown_after_send"])
def test_later_batch_cannot_spend_unsent_normal_prefix(session, prior_topic_status):
    _freeze(session, [1, 2, 3])
    intents = list(session.scalars(select(AiGroupContentIntent).order_by(
        AiGroupContentIntent.normal_text_ordinal)))
    for index, intent in enumerate(intents, 1):
        _confirm_remote(session, intent, index)
    fourth = _freeze(session, [4])[0]["slot"]
    assert fourth["topic_mode"] == "configured_topic"
    session.add(Action(id="reserved-topic", tenant_id=1, task_id="allocation-task",
        task_type="group_ai_chat", action_type="send_message",
        primary_quantity_slot_id="quantity-4", status=prior_topic_status))
    session.flush()

    later = _freeze(session, [5, 6, 7])

    assert [row["slot"]["topic_mode"] for row in later] == ["group_free_chat"] * 3
    if prior_topic_status == "pending":
        validate_content_intent_for_gateway(session, _payload_for_slot(session, fourth),
            remote_boundary=True)
    assert session.get(Action, "reserved-topic").status == prior_topic_status


def test_zero_confirmed_cross_batch_still_plans_all_quantity_slots(session):
    first = _freeze(session, [1, 2, 3])
    later = _freeze(session, [4, 5, 6, 7])

    assert len(first) + len(later) == 7
    assert all(row["slot"]["topic_mode"] == "group_free_chat" for row in later)
    assert all(not row["slot"]["topic_capacity_reservation_id"] for row in later)
    assert session.query(AiGroupContentIntent).count() == 7
