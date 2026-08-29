from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.models import (
    Action,
    ExecutionAttempt,
    GatewayRequestEvidenceJournal,
)


CHANNEL_REMOTE_ACTION_TYPES = frozenset({"like_message", "view_message"})


@dataclass(frozen=True)
class ActionRemoteMutationEvidence:
    state: str | None
    representative_attempt: ExecutionAttempt | None
    gateway_attempt_count: int


def action_remote_mutation_evidence(
    session: Session,
    action: Action,
) -> ActionRemoteMutationEvidence:
    attempts = list(session.scalars(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.action_id == action.id)
        .order_by(ExecutionAttempt.attempt_no.desc())
        .with_for_update()
    ))
    latest = attempts[0] if attempts else None
    gateway_attempts = [
        attempt for attempt in attempts
        if attempt.gateway_call_started_at is not None
    ]
    if not gateway_attempts:
        return ActionRemoteMutationEvidence(None, latest, 0)
    evaluated = [
        (attempt, remote_mutation_state(action, attempt))
        for attempt in gateway_attempts
    ]
    unsafe = [item for item in evaluated if item[1] != "false"]
    if not unsafe:
        return ActionRemoteMutationEvidence(
            "false",
            gateway_attempts[0],
            len(gateway_attempts),
        )
    representative = next(
        (item for item in unsafe if item[1] == "unknown"),
        unsafe[0],
    )
    return ActionRemoteMutationEvidence(
        representative[1],
        representative[0],
        len(gateway_attempts),
    )


def remote_mutation_state(
    action: Action,
    attempt: ExecutionAttempt,
) -> str:
    journal_state = _journal_mutation_state(action, attempt)
    if journal_state in {"false", "true"}:
        return journal_state
    observed = dict(attempt.result_snapshot or {}).get("remote_mutation_started")
    result = dict(action.result or {})
    if result.get("callback_mutation_started") is True:
        return "unknown"
    if result.get("remote_mutation_started") is True or observed is True:
        return "true"
    if observed is False:
        return "false"
    return "unknown"


def _journal_mutation_state(
    action: Action,
    attempt: ExecutionAttempt,
) -> str:
    session = object_session(action)
    if session is None:
        return "unknown"
    journal = session.scalar(
        select(GatewayRequestEvidenceJournal)
        .where(
            GatewayRequestEvidenceJournal.action_id == action.id,
            GatewayRequestEvidenceJournal.execution_attempt_id == attempt.id,
        )
        .limit(1)
    )
    return str(journal.remote_mutation_state or "unknown") if journal else "unknown"


__all__ = [
    "ActionRemoteMutationEvidence",
    "CHANNEL_REMOTE_ACTION_TYPES",
    "action_remote_mutation_evidence",
    "remote_mutation_state",
]
