from __future__ import annotations

import socket
from collections.abc import Callable
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Action, Task, TgAccount
from app.services._common import _now
from app.services.developer_apps import credentials_for_account

from .ai_generation_composition import PRODUCTION_GENERATION_DEPENDENCIES
from .ai_generation_dependencies import GenerationDependencies
from .ai_generation_dispatch import ensure_send_message_content
from .payloads import SendMessagePayload


GENERATION_LOOKAHEAD = timedelta(minutes=30)
GENERATION_LEASE = timedelta(minutes=10)
GENERATION_BATCH_SIZE = 10
GENERATABLE_STATUSES = ("pending", "ai_result_persist_unknown")
GenerateAction = Callable[[Session, Action, TgAccount], None]


def drain_ai_generation(
    session_factory,
    limit: int = 20,
    *,
    generate_action: GenerateAction | None = None,
    dependencies: GenerationDependencies = PRODUCTION_GENERATION_DEPENDENCIES,
) -> int:
    processor = generate_action or _production_generate_action(dependencies)
    owner = f"ai-generation:{socket.gethostname()}:{uuid4()}"
    processed = 0
    while processed < max(1, int(limit)):
        claim = _claim_generation_batch(
            session_factory,
            owner,
            max_batch_size=min(GENERATION_BATCH_SIZE, max(1, int(limit)) - processed),
        )
        if claim is None:
            break
        action_id, token = claim
        with session_factory() as session:
            action = session.get(Action, action_id)
            if not _owns_generation_claim(action, owner, token):
                raise RuntimeError(f"AI generation claim lost for action {action_id}")
            account = session.get(TgAccount, action.account_id)
            if account is None:
                raise RuntimeError(f"AI generation action {action.id} has no account")
            processor(session, action, account)
        processed += _release_prepared_batch(session_factory, owner, token)
    return processed


def _claim_generation_batch(
    session_factory,
    owner: str,
    *,
    max_batch_size: int,
) -> tuple[str, str] | None:
    with session_factory() as session:
        first = session.scalar(_claim_statement(session, _generation_filters()).limit(1))
        if first is None:
            return None
        token = str(uuid4())
        batch = [first, *_generation_siblings(session, first, max_batch_size - 1)]
        for action in batch:
            _mark_generation_claim(action, owner, token)
        session.commit()
        return first.id, token


def _claim_statement(session: Session, filters: tuple):
    statement = (
        select(Action)
            .join(Task, Task.id == Action.task_id)
            .where(*filters)
            .order_by(Action.scheduled_at.asc(), Action.created_at.asc(), Action.id.asc())
    )
    if session.bind is not None and session.bind.dialect.name != "sqlite":
        return statement.with_for_update(skip_locked=True, of=Action)
    return statement


def _generation_siblings(
    session: Session,
    first: Action,
    limit: int,
) -> list[Action]:
    payload = first.payload if isinstance(first.payload, dict) else {}
    generation_id = str(payload.get("ai_generation_id") or "")
    if limit <= 0 or not generation_id or payload.get("reply_to_message_id"):
        return []
    filters = (
        *_generation_filters(),
        Action.id != first.id,
        Action.task_id == first.task_id,
        Action.payload["ai_generation_id"].as_string() == generation_id,
        or_(
            Action.payload["reply_to_message_id"].as_integer().is_(None),
            Action.payload["reply_to_message_id"].as_integer() == 0,
        ),
    )
    return list(session.scalars(_claim_statement(session, filters).limit(limit)))


def _generation_filters() -> tuple:
    payload_status = Action.payload["ai_generation_status"].as_string()
    message_text = Action.payload["message_text"].as_string()
    return (
        Action.task_type == "group_ai_chat",
        Action.action_type == "send_message",
        Action.status == "pending",
        Action.account_id.is_not(None),
        Action.scheduled_at <= _now() + GENERATION_LOOKAHEAD,
        Task.status == "running",
        Task.deleted_at.is_(None),
        payload_status.in_(GENERATABLE_STATUSES),
        func.coalesce(message_text, "") == "",
    )


def _mark_generation_claim(action: Action, owner: str, token: str) -> None:
    payload = dict(action.payload) if isinstance(action.payload, dict) else {}
    payload["ai_generation_claim_owner"] = owner
    payload["ai_generation_claim_token"] = token
    action.payload = payload
    action.status = "executing"
    action.claim_owner = owner
    action.claim_token = token
    action.lease_owner = owner
    action.lease_expires_at = _now() + GENERATION_LEASE


def _owns_generation_claim(
    action: Action | None,
    owner: str,
    token: str,
) -> bool:
    return bool(
        action
        and action.status == "executing"
        and action.claim_owner == owner
        and action.claim_token == token
    )


def _release_prepared_batch(session_factory, owner: str, token: str) -> int:
    with session_factory() as session:
        actions = list(session.scalars(select(Action).where(
            Action.status == "executing",
            Action.claim_owner == owner,
            Action.claim_token == token,
        )))
        for action in actions:
            payload = action.payload if isinstance(action.payload, dict) else {}
            if not str(payload.get("message_text") or "").strip():
                raise RuntimeError(f"AI generation action {action.id} completed without content")
            _release_generation_claim(action, payload)
        session.commit()
        return len(actions)


def _release_generation_claim(action: Action, payload: dict) -> None:
    payload = dict(payload)
    payload["ai_generation_claim_owner"] = ""
    payload["ai_generation_claim_token"] = ""
    action.payload = payload
    action.status = "pending"
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.lease_owner = ""
    action.lease_expires_at = None
    action.executed_at = None


def _production_generate_action(
    dependencies: GenerationDependencies,
) -> GenerateAction:
    def generate(session: Session, action: Action, account: TgAccount) -> None:
        ensure_send_message_content(
            session,
            action,
            account,
            payload=SendMessagePayload.model_validate(action.payload or {}),
            credentials=credentials_for_account(session, account),
            dependencies=dependencies,
        )

    return generate


__all__ = ["drain_ai_generation"]
