from __future__ import annotations

import socket
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, Task
from app.services._common import _now

from .ai_generation_claim_lifecycle import (
    mark_generation_claim,
    owns_generation_claim,
    release_generation_claim,
)
from .ai_generation_state import GenerationAttemptStale
from .ai_generation_timing import GENERATION_LOOKAHEAD
from .ai_generator import AiGenerationUnavailable, ProviderRouteDeferred
from .channel_payloads import PostCommentPayload
from .comment_generation_dispatch import (
    PRODUCTION_COMMENT_GENERATION_DEPENDENCIES,
    ensure_post_comment_content,
)
from .comment_generation_pipeline import CommentGenerationDependencies
from .runtime_resources import _release_runtime_resources


COMMENT_GENERATABLE_STATUSES = ("pending", "ai_result_persist_unknown")


@dataclass(frozen=True)
class CommentGenerationClaim:
    action_id: str
    owner: str
    token: str


def drain_comment_generation(
    session_factory,
    limit: int = 20,
    *,
    dependencies: CommentGenerationDependencies = PRODUCTION_COMMENT_GENERATION_DEPENDENCIES,
) -> int:
    owner = f"comment-generation:{socket.gethostname()}:{uuid4()}"
    processed = 0
    while processed < max(1, int(limit)):
        claim = _claim_comment_generation(session_factory, owner=owner)
        if claim is None:
            break
        _process_comment_generation(
            session_factory, claim, dependencies=dependencies,
        )
        processed += 1
    return processed


def _claim_comment_generation(
    session_factory,
    *,
    owner: str,
) -> CommentGenerationClaim | None:
    with session_factory() as session:
        statement = _comment_candidate_statement(session)
        action = session.scalar(statement)
        if action is None:
            return None
        token = str(uuid4())
        mark_generation_claim(action, owner, token)
        session.commit()
        return CommentGenerationClaim(action.id, owner, token)


def _comment_candidate_statement(session: Session):
    status = Action.payload["ai_generation_status"].as_string()
    text = Action.payload["comment_text"].as_string()
    statement = (
        select(Action)
        .join(Task, Task.id == Action.task_id)
        .where(
            Action.task_type == "channel_comment",
            Action.action_type == "post_comment",
            Action.status == "pending",
            Action.account_id.is_not(None),
            Action.scheduled_at <= _now() + GENERATION_LOOKAHEAD,
            Task.status == "running",
            Task.deleted_at.is_(None),
            status.in_(COMMENT_GENERATABLE_STATUSES),
            func.coalesce(text, "") == "",
        )
        .order_by(Action.scheduled_at, Action.created_at, Action.id)
        .limit(1)
    )
    if session.bind is not None and session.bind.dialect.name != "sqlite":
        return statement.with_for_update(skip_locked=True, of=Action)
    return statement


def _process_comment_generation(
    session_factory,
    claim: CommentGenerationClaim,
    *,
    dependencies: CommentGenerationDependencies,
) -> None:
    expected_failure = False
    with session_factory() as session:
        action = session.get(Action, claim.action_id)
        if not owns_generation_claim(action, claim.owner, claim.token):
            raise RuntimeError("comment_generation_action_claim_lost")
        payload = PostCommentPayload.model_validate(action.payload or {})
        try:
            ensure_post_comment_content(
                session, action, payload=payload, dependencies=dependencies,
            )
        except (AiGenerationUnavailable, ProviderRouteDeferred, GenerationAttemptStale):
            session.rollback()
            expected_failure = True
        else:
            session.commit()
    _release_comment_generation_claim(session_factory, claim)
    if expected_failure:
        return


def _release_comment_generation_claim(
    session_factory,
    claim: CommentGenerationClaim,
) -> None:
    with session_factory() as session:
        action = session.get(Action, claim.action_id)
        if not owns_generation_claim(action, claim.owner, claim.token):
            _release_runtime_resources(action)
            return
        release_generation_claim(action, dict(action.payload or {}))
        session.commit()
        _release_runtime_resources(action)


__all__ = ["drain_comment_generation"]
