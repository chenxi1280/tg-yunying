from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiGroupContentIntent,
    FulfillmentRemoteFact,
    TaskGroupDailyMessageSlot,
)


ACTIVE_TOPIC_ACTION_STATUSES = frozenset(
    {"pending", "claiming", "executing", "retryable_failed"}
)
TOPIC_UNKNOWN_ACTION_STATUS = "unknown_after_send"
REMOTE_MESSAGE_FACT_KIND = "remote_message_observed"
INTENT_STATE_PRECEDENCE = {
    "released": 0,
    "active": 1,
    "unknown": 2,
    "confirmed": 3,
}


@dataclass(frozen=True)
class TopicCapacityProjection:
    confirmed_normal_count: int = 0
    confirmed_topic_count: int = 0
    unknown_topic_count: int = 0
    active_topic_reservations: int = 0
    active_normal_reservations: int = 0


def topic_capacity_projection(
    session: Session,
    allocation_plan_id: str,
) -> TopicCapacityProjection:
    confirmed_normal = confirmed_topic = unknown_topic = 0
    active_topic = active_normal = 0
    for intent, state in plan_intent_remote_states(session, allocation_plan_id):
        is_topic = intent.topic_mode == "configured_topic"
        if state == "confirmed":
            confirmed_normal += 1
            confirmed_topic += int(is_topic)
        elif is_topic and state == "unknown":
            unknown_topic += 1
        elif state == "active":
            active_normal += 1
            active_topic += int(is_topic)
    return TopicCapacityProjection(
        confirmed_normal,
        confirmed_topic,
        unknown_topic,
        active_topic,
        active_normal,
    )


def plan_intent_remote_states(
    session: Session,
    allocation_plan_id: str,
) -> list[tuple[AiGroupContentIntent, str]]:
    rows = session.execute(
        select(
            AiGroupContentIntent,
            TaskGroupDailyMessageSlot,
            Action,
            FulfillmentRemoteFact.fact_id,
        )
        .outerjoin(
            TaskGroupDailyMessageSlot,
            TaskGroupDailyMessageSlot.id
            == AiGroupContentIntent.primary_quantity_slot_id,
        )
        .outerjoin(
            Action,
            Action.primary_quantity_slot_id
            == AiGroupContentIntent.primary_quantity_slot_id,
        )
        .outerjoin(
            FulfillmentRemoteFact,
            (FulfillmentRemoteFact.action_id == Action.id)
            & (FulfillmentRemoteFact.fact_kind == REMOTE_MESSAGE_FACT_KIND),
        )
        .where(AiGroupContentIntent.allocation_plan_id == allocation_plan_id)
    )
    intents: dict[str, AiGroupContentIntent] = {}
    states: dict[str, str] = {}
    for intent, quantity_slot, action, fact_id in rows:
        state = intent_remote_state(quantity_slot, action, fact_id)
        intents[intent.id] = intent
        if intent.id not in states or _is_stronger_state(state, states[intent.id]):
            states[intent.id] = state
    return [(intents[intent_id], states[intent_id]) for intent_id in intents]


def _is_stronger_state(candidate: str, current: str) -> bool:
    return INTENT_STATE_PRECEDENCE[candidate] > INTENT_STATE_PRECEDENCE[current]


def intent_remote_state(
    quantity_slot: TaskGroupDailyMessageSlot | None,
    action: Action | None,
    fact_id: str | None,
) -> str:
    if fact_id:
        return "confirmed"
    if action is not None and action.status == TOPIC_UNKNOWN_ACTION_STATUS:
        return "unknown"
    if action is not None and action.status in ACTIVE_TOPIC_ACTION_STATUSES:
        return "active"
    if quantity_slot is not None and quantity_slot.state != "terminal":
        return "active"
    return "released"


__all__ = [
    "ACTIVE_TOPIC_ACTION_STATUSES",
    "REMOTE_MESSAGE_FACT_KIND",
    "TOPIC_UNKNOWN_ACTION_STATUS",
    "TopicCapacityProjection",
    "intent_remote_state",
    "plan_intent_remote_states",
    "topic_capacity_projection",
]
