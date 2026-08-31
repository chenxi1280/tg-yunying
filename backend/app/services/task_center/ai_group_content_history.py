from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiGroupContentAllocationPlan,
    AiGroupContentIntent,
    FulfillmentRemoteFact,
    TaskGroupDailyMessageSlot,
)

from .ai_group_content_projection import (
    INTENT_STATE_PRECEDENCE,
    REMOTE_MESSAGE_FACT_KIND,
    intent_remote_state,
)


HISTORY_QUERY_LIMIT = 500


@dataclass(frozen=True)
class ContentHistoryMessage:
    intent: AiGroupContentIntent
    state: str
    sample_ids: tuple[str, ...]
    term_ids: tuple[str, ...]
    effective_at: datetime | None


def active_history_message(intent: AiGroupContentIntent) -> ContentHistoryMessage:
    return ContentHistoryMessage(
        intent,
        "active",
        tuple(str(value) for value in intent.vocabulary_sample_ids),
        tuple(str(value) for value in intent.vocabulary_normalized_term_ids),
        intent.created_at,
    )


def recent_plan_histories(
    session: Session,
    plan: AiGroupContentAllocationPlan,
    *,
    limit: int,
) -> tuple[list[ContentHistoryMessage], list[ContentHistoryMessage]]:
    surface = recent_content_history(
        session, plan.surface_scope_key, include_route_family=True, limit=limit
    )
    group = recent_content_history(
        session, plan.surface_scope_key, include_route_family=False, limit=limit
    )
    return surface, group


def recent_content_history(
    session: Session,
    surface_scope_key: str,
    *,
    include_route_family: bool,
    limit: int,
) -> list[ContentHistoryMessage]:
    statement = _history_statement(surface_scope_key, include_route_family)
    rows = session.execute(statement.limit(HISTORY_QUERY_LIMIT))
    selected: dict[str, ContentHistoryMessage] = {}
    for intent, quantity, action, fact_id, observed_at in rows:
        candidate = _history_message(intent, quantity, action, fact_id, observed_at)
        current = selected.get(intent.id)
        if candidate.state == "released":
            continue
        if current is None or _stronger_history(candidate, current):
            selected[intent.id] = candidate
    return sorted(
        selected.values(),
        key=lambda item: (_time_key(item.effective_at), item.intent.id),
        reverse=True,
    )[:limit]


def _history_statement(surface_scope_key: str, include_route_family: bool):
    scope_filter = AiGroupContentAllocationPlan.surface_scope_key == surface_scope_key
    if not include_route_family:
        prefix = surface_scope_key.rsplit(":route:", 1)[0] + ":route:"
        scope_filter = AiGroupContentAllocationPlan.surface_scope_key.like(f"{prefix}%")
    return (
        select(
            AiGroupContentIntent,
            TaskGroupDailyMessageSlot,
            Action,
            FulfillmentRemoteFact.fact_id,
            FulfillmentRemoteFact.observed_at,
        )
        .join(AiGroupContentAllocationPlan)
        .outerjoin(
            TaskGroupDailyMessageSlot,
            TaskGroupDailyMessageSlot.id == AiGroupContentIntent.primary_quantity_slot_id,
        )
        .outerjoin(
            Action,
            Action.primary_quantity_slot_id == AiGroupContentIntent.primary_quantity_slot_id,
        )
        .outerjoin(
            FulfillmentRemoteFact,
            (FulfillmentRemoteFact.action_id == Action.id)
            & (FulfillmentRemoteFact.fact_kind == REMOTE_MESSAGE_FACT_KIND),
        )
        .where(scope_filter)
        .order_by(AiGroupContentIntent.created_at.desc(), AiGroupContentIntent.id.desc())
    )


def _history_message(
    intent: AiGroupContentIntent,
    quantity: TaskGroupDailyMessageSlot | None,
    action: Action | None,
    fact_id: str | None,
    observed_at: datetime | None,
) -> ContentHistoryMessage:
    state = intent_remote_state(quantity, action, fact_id)
    payload = dict(action.payload or {}) if action is not None else {}
    if state in {"confirmed", "unknown"}:
        sample_ids = _strings(payload.get("vocabulary_used_ids"))
        term_ids = _strings(payload.get("vocabulary_used_term_ids"))
    else:
        sample_ids = tuple(str(value) for value in intent.vocabulary_sample_ids)
        term_ids = tuple(str(value) for value in intent.vocabulary_normalized_term_ids)
    effective_at = observed_at or getattr(action, "executed_at", None)
    effective_at = effective_at or getattr(action, "created_at", None) or intent.created_at
    return ContentHistoryMessage(intent, state, sample_ids, term_ids, effective_at)


def _stronger_history(candidate: ContentHistoryMessage, current: ContentHistoryMessage) -> bool:
    candidate_key = (INTENT_STATE_PRECEDENCE[candidate.state], _time_key(candidate.effective_at))
    current_key = (INTENT_STATE_PRECEDENCE[current.state], _time_key(current.effective_at))
    return candidate_key > current_key


def _time_key(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _strings(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item))


__all__ = [
    "ContentHistoryMessage",
    "active_history_message",
    "recent_content_history",
    "recent_plan_histories",
]
