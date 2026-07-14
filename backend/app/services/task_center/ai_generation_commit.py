from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session, attributes

from app.models import Action

from .ai_generation_state import GenerationAttemptStale
from .payloads import SendMessagePayload


def load_generation_batch(session: Session, request) -> list[tuple[Action, SendMessagePayload]]:
    rows = list(session.scalars(select(Action).where(
        Action.id.in_(request.batch_ids),
        Action.tenant_id == request.tenant_id,
        Action.task_id == request.task_id,
        Action.status == "executing",
        Action.payload["ai_generation_claim_owner"].as_string() == request.claim_owner,
        Action.payload["ai_generation_claim_token"].as_string() == request.claim_token,
    )))
    actions = {action.id: action for action in rows}
    if set(actions) != set(request.batch_ids):
        raise GenerationAttemptStale("ai_generation_attempt_stale")
    batch = [
        (actions[action_id], SendMessagePayload.model_validate(actions[action_id].payload or {}))
        for action_id in request.batch_ids
    ]
    if any(not _attempt_matches(request, payload) for _action, payload in batch):
        raise GenerationAttemptStale("ai_generation_attempt_stale")
    return batch


def _attempt_matches(request, payload: SendMessagePayload) -> bool:
    return (
        payload.ai_generation_attempt_id == request.attempt_id
        and int(payload.group_id or 0) == request.group_id
    )


def commit_generation_action(session: Session, request, action: Action) -> None:
    values = _generation_action_values(action)
    statement = (
        update(Action)
        .where(
            Action.id == action.id,
            Action.tenant_id == request.tenant_id,
            Action.task_id == request.task_id,
            Action.status == "executing",
            Action.payload["ai_generation_claim_owner"].as_string() == request.claim_owner,
            Action.payload["ai_generation_claim_token"].as_string() == request.claim_token,
            Action.payload["ai_generation_attempt_id"].as_string() == request.attempt_id,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    with session.no_autoflush:
        result = session.execute(statement)
    if result.rowcount != 1:
        raise GenerationAttemptStale("ai_generation_attempt_stale")
    for field, value in values.items():
        attributes.set_committed_value(action, field, value)


def _generation_action_values(action: Action) -> dict:
    return {
        "payload": action.payload,
        "result": action.result,
        "status": action.status,
        "executed_at": action.executed_at,
        "claim_owner": action.claim_owner,
        "claim_token": action.claim_token,
        "claim_expires_at": action.claim_expires_at,
        "lease_owner": action.lease_owner,
        "lease_expires_at": action.lease_expires_at,
    }


__all__ = ["commit_generation_action", "load_generation_batch"]
