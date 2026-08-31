from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select
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
QUERY_LIMIT = 500


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
    statement = (
        select(Action, FulfillmentRemoteFact.fact_id)
        .outerjoin(
            FulfillmentRemoteFact,
            (FulfillmentRemoteFact.action_id == Action.id)
            & (FulfillmentRemoteFact.fact_kind == REMOTE_MESSAGE_FACT_KIND),
        )
        .where(
            Action.tenant_id == action.tenant_id,
            Action.task_type == "group_ai_chat",
            Action.id != action.id,
            Action.payload["surface_scope_key"].as_string() == surface_scope_key,
        )
        .order_by(
            func.coalesce(
                FulfillmentRemoteFact.observed_at,
                Action.executed_at,
                Action.created_at,
            ).desc(),
            Action.id.desc(),
        )
        .limit(QUERY_LIMIT)
    )
    result: list[dict] = []
    seen: set[str] = set()
    for historical, fact_id in session.execute(statement):
        if historical.id in seen:
            continue
        seen.add(historical.id)
        if not fact_id and historical.status not in ACTIVE_RESERVATION_STATUSES:
            continue
        historical_payload = dict(historical.payload or {})
        if not historical_payload.get("allocation_plan_id"):
            continue
        result.append(historical_payload)
        if len(result) >= VOCABULARY_WINDOW - 1:
            break
    return result


def _strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


__all__ = [
    "vocabulary_frequency_baseline",
    "vocabulary_frequency_violation",
    "vocabulary_frequency_violation_from_rows",
]
