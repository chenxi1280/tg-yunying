from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, attributes

from app.models import Action

from .ai_generation_state import GenerationAttemptStale


def load_attempt_action(session: Session, request: Any) -> Action:
    action = session.scalar(select(Action).where(
        Action.id == request.action_id,
        Action.tenant_id == request.tenant_id,
        Action.task_id == request.task_id,
        Action.status == "executing",
        Action.payload["ai_generation_claim_owner"].as_string() == request.claim_owner,
        Action.payload["ai_generation_claim_token"].as_string() == request.claim_token,
        Action.payload["ai_generation_attempt_id"].as_string() == request.attempt_id,
    ))
    if action is None:
        raise GenerationAttemptStale("ai_generation_attempt_stale")
    return action


def cas_write_action(session: Session, request: Any, action: Action) -> None:
    values = _action_values(action)
    statement = update(Action).where(
        Action.id == request.action_id,
        Action.tenant_id == request.tenant_id,
        Action.task_id == request.task_id,
        Action.status == "executing",
        Action.payload["ai_generation_claim_owner"].as_string() == request.claim_owner,
        Action.payload["ai_generation_claim_token"].as_string() == request.claim_token,
        Action.payload["ai_generation_attempt_id"].as_string() == request.attempt_id,
    ).values(**values).execution_options(synchronize_session=False)
    with session.no_autoflush:
        result = session.execute(statement)
    if result.rowcount != 1:
        raise GenerationAttemptStale("ai_generation_attempt_stale")
    for field, value in values.items():
        attributes.set_committed_value(action, field, value)


def _action_values(action: Action) -> dict:
    return {
        "payload": action.payload,
        "result": action.result,
        "status": action.status,
        "scheduled_at": action.scheduled_at,
        "executed_at": action.executed_at,
        "claim_owner": action.claim_owner,
        "claim_token": action.claim_token,
        "claim_expires_at": action.claim_expires_at,
        "lease_owner": action.lease_owner,
        "lease_expires_at": action.lease_expires_at,
    }
