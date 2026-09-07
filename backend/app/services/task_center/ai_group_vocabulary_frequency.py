from __future__ import annotations

from collections import Counter

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Action, FulfillmentRemoteFact

from .payloads import SendMessagePayload


REMOTE_MESSAGE_FACT_KIND = "remote_message_observed"
ACTIVE_RESERVATION_STATUSES = frozenset(
    {"pending", "claiming", "executing", "retryable_failed", "unknown_after_send"}
)
VOCABULARY_WINDOW = 100
PHRASE_WINDOW = 20
MAX_TERM_OCCURRENCES = 5
MAX_PHRASE_OCCURRENCES = 2


def vocabulary_frequency_violation(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
    *,
    data: dict,
) -> str:
    if not payload.allocation_plan_id or not payload.surface_scope_key:
        return ""
    rows = vocabulary_frequency_baseline(session, action, payload)
    return vocabulary_frequency_violation_from_rows(rows, data=data)


def vocabulary_frequency_baseline(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
) -> list[dict]:
    if not payload.allocation_plan_id or not payload.surface_scope_key:
        return []
    return _eligible_history(session, action, payload.surface_scope_key)


def vocabulary_frequency_violation_from_rows(rows: list[dict], *, data: dict) -> str:
    term_rows = rows[: VOCABULARY_WINDOW - 1]
    vocabulary_counts = Counter(
        vocabulary_id
        for row in term_rows
        for vocabulary_id in _strings(row.get("vocabulary_used_ids"))
    )
    for vocabulary_id in _strings(data.get("vocabulary_used_ids")):
        if vocabulary_counts[vocabulary_id] >= MAX_TERM_OCCURRENCES:
            return f"vocabulary_id:{vocabulary_id}"
    term_counts = Counter(
        term
        for row in term_rows
        for term in _strings(row.get("vocabulary_used_term_ids"))
    )
    for term in _strings(data.get("vocabulary_used_term_ids")):
        if term_counts[term] >= MAX_TERM_OCCURRENCES:
            return f"normalized_term:{term}"
    phrase_rows = rows[: PHRASE_WINDOW - 1]
    phrase_counts = Counter(
        fingerprint
        for row in phrase_rows
        for fingerprint in _strings(row.get("surface_phrase_fingerprints"))
    )
    for fingerprint in _strings(data.get("surface_phrase_fingerprints")):
        if phrase_counts[fingerprint] >= MAX_PHRASE_OCCURRENCES:
            return f"surface_2gram:{fingerprint}"
    return ""


def _eligible_history(
    session: Session,
    action: Action,
    surface_scope_key: str,
) -> list[dict]:
    return list(session.scalars(_history_statement(action, surface_scope_key)))


def _history_statement(action: Action, surface_scope_key: str):
    observed_at = (
        select(func.max(FulfillmentRemoteFact.observed_at))
        .where(
            FulfillmentRemoteFact.tenant_id == Action.tenant_id,
            FulfillmentRemoteFact.action_id == Action.id,
            FulfillmentRemoteFact.fact_kind == REMOTE_MESSAGE_FACT_KIND,
        )
        .correlate(Action)
        .scalar_subquery()
    )
    return (
        select(Action.payload)
        .where(
            Action.tenant_id == action.tenant_id,
            Action.task_type == "group_ai_chat",
            Action.id != action.id,
            Action.payload["surface_scope_key"].as_string() == surface_scope_key,
            func.coalesce(Action.payload["allocation_plan_id"].as_string(), "") != "",
            or_(Action.status.in_(ACTIVE_RESERVATION_STATUSES), observed_at.is_not(None)),
        )
        .order_by(
            func.coalesce(observed_at, Action.executed_at, Action.created_at).desc(),
            Action.id.desc(),
        )
        .limit(VOCABULARY_WINDOW - 1)
    )


def _strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


__all__ = [
    "vocabulary_frequency_baseline",
    "vocabulary_frequency_violation",
    "vocabulary_frequency_violation_from_rows",
]
