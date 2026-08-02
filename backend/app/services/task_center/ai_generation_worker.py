from __future__ import annotations

import socket
from collections.abc import Callable
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models import Action, Task, TgAccount, TgGroup
from app.services._common import _now
from app.services.developer_apps import credentials_for_account

from .ai_generation_composition import PRODUCTION_GENERATION_DEPENDENCIES
from .ai_generation_dependencies import GenerationDependencies
from .ai_generation_dispatch import ensure_send_message_content
from .ai_generator import AiGenerationUnavailable
from .ai_generation_timing import GENERATION_LEASE, GENERATION_LOOKAHEAD
from .payloads import SendMessagePayload


GENERATABLE_STATUSES = ("pending", "ai_result_persist_unknown")
GROUP_BOT_ADMISSION_RETRY_SECONDS = 30
GenerateAction = Callable[[Session, Action, TgAccount], None]


class GenerationAdmissionDeferred(RuntimeError):
    pass


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
    visited_action_ids: set[str] = set()
    while processed < max(1, int(limit)):
        claim = _claim_generation_batch(
            session_factory,
            owner,
            excluded_action_ids=visited_action_ids,
        )
        if claim is None:
            break
        action_id, token, claimed_count = claim
        visited_action_ids.add(action_id)
        generation_failure: AiGenerationUnavailable | None = None
        admission_deferred = False
        with session_factory() as session:
            action = session.get(Action, action_id)
            if not _owns_generation_claim(action, owner, token):
                raise RuntimeError(f"AI generation claim lost for action {action_id}")
            account = session.get(TgAccount, action.account_id)
            if account is None:
                raise RuntimeError(f"AI generation action {action.id} has no account")
            try:
                processor(session, action, account)
            except GenerationAdmissionDeferred:
                session.rollback()
                admission_deferred = True
            except AiGenerationUnavailable as exc:
                session.rollback()
                generation_failure = exc
        if admission_deferred:
            _release_unprepared_batch(session_factory, owner, token)
            processed += claimed_count
            continue
        if generation_failure is not None:
            if not _persisted_generation_failure(
                session_factory, action_id,
            ):
                raise generation_failure
            _release_unprepared_batch(session_factory, owner, token)
            processed += claimed_count
            continue
        processed += _release_prepared_batch(session_factory, owner, token)
    return processed


def _claim_generation_batch(
    session_factory,
    owner: str,
    *,
    excluded_action_ids: set[str],
) -> tuple[str, str, int] | None:
    with session_factory() as session:
        first = session.scalar(_claim_statement(
            session,
            _generation_filters(excluded_action_ids=excluded_action_ids),
        ).limit(1))
        if first is None:
            return None
        _lock_generation_group(session, first)
        if not _generation_group_is_free(session, first):
            session.rollback()
            return None
        token = str(uuid4())
        _mark_generation_claim(first, owner, token)
        session.commit()
        return first.id, token, 1


def _lock_generation_group(session: Session, action: Action) -> None:
    group_id = int((action.payload or {}).get("group_id") or 0)
    locked = session.scalar(
        select(TgGroup.id)
        .where(
            TgGroup.id == group_id,
            TgGroup.tenant_id == action.tenant_id,
        )
        .with_for_update()
    )
    if locked is None:
        raise RuntimeError(f"AI generation action {action.id} has no target group")


def _generation_group_is_free(session: Session, action: Action) -> bool:
    occupied = aliased(Action)
    occupied_status = occupied.payload["ai_generation_status"].as_string()
    occupied_text = occupied.payload["message_text"].as_string()
    group_id = int((action.payload or {}).get("group_id") or 0)
    occupant = session.scalar(select(occupied.id).where(
        occupied.id != action.id,
        occupied.tenant_id == action.tenant_id,
        occupied.task_type == "group_ai_chat",
        occupied.action_type == "send_message",
        occupied.payload["group_id"].as_integer() == group_id,
        or_(
            and_(occupied.status == "executing", occupied_status == "generating"),
            and_(
                occupied.status.in_(("pending", "claiming", "executing")),
                occupied_status == "ready",
                func.coalesce(occupied_text, "") != "",
            ),
        ),
    ).limit(1))
    return occupant is None


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


def _generation_filters(*, excluded_action_ids: set[str]) -> tuple:
    payload_status = Action.payload["ai_generation_status"].as_string()
    message_text = Action.payload["message_text"].as_string()
    executing = aliased(Action)
    account_is_free = ~select(executing.id).where(
        executing.account_id == Action.account_id,
        executing.status == "executing",
    ).exists()
    group_is_free = _group_generation_slot_is_free()
    filters = (
        Action.task_type == "group_ai_chat",
        Action.action_type == "send_message",
        Action.status == "pending",
        Action.account_id.is_not(None),
        Action.scheduled_at <= _now() + GENERATION_LOOKAHEAD,
        Task.status == "running",
        Task.deleted_at.is_(None),
        payload_status.in_(GENERATABLE_STATUSES),
        func.coalesce(message_text, "") == "",
        account_is_free,
        group_is_free,
    )
    if not excluded_action_ids:
        return filters
    return (*filters, Action.id.not_in(excluded_action_ids))


def _group_generation_slot_is_free():
    occupied = aliased(Action)
    occupied_status = occupied.payload["ai_generation_status"].as_string()
    occupied_text = occupied.payload["message_text"].as_string()
    same_group = (
        occupied.payload["group_id"].as_integer()
        == Action.payload["group_id"].as_integer()
    )
    generating = and_(
        occupied.status == "executing",
        occupied_status == "generating",
    )
    ready = and_(
        occupied.status.in_(("pending", "claiming", "executing")),
        occupied_status == "ready",
        func.coalesce(occupied_text, "") != "",
    )
    return ~select(occupied.id).where(
        occupied.id != Action.id,
        occupied.tenant_id == Action.tenant_id,
        occupied.task_type == "group_ai_chat",
        occupied.action_type == "send_message",
        same_group,
        or_(generating, ready),
    ).exists()


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


def _release_unprepared_batch(
    session_factory,
    owner: str,
    token: str,
) -> None:
    with session_factory() as session:
        actions = list(session.scalars(select(Action).where(
            Action.status == "executing",
            Action.claim_owner == owner,
            Action.claim_token == token,
        )))
        for action in actions:
            _release_generation_claim(action, dict(action.payload or {}))
        session.commit()


def _persisted_generation_failure(
    session_factory,
    action_id: str,
) -> bool:
    with session_factory() as session:
        action = session.get(Action, action_id)
        result = action.result if action and isinstance(action.result, dict) else {}
        persisted = bool(
            action
            and str(result.get("error_code") or "")
            and (
                action.status in {"failed", "skipped"}
                or (
                    action.status == "pending"
                    and result.get("error_code") == "context_freshness_unproven"
                )
            )
        )
        if persisted and action.status in {"failed", "skipped"}:
            from . import dispatcher
            from .conversation_speaker_rotation import release_group_ai_speaker_reservation

            release_group_ai_speaker_reservation(session, action)
            dispatcher._sync_action_content_mix_state(session, action)
            session.commit()
        return persisted


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
        payload = SendMessagePayload.model_validate(action.payload or {})
        if _defer_generation_for_group_bot_admission(
            session,
            action,
            payload,
        ):
            raise GenerationAdmissionDeferred(action.id)
        ensure_send_message_content(
            session,
            action,
            account,
            payload=SendMessagePayload.model_validate(action.payload or {}),
            credentials=credentials_for_account(session, account),
            dependencies=dependencies,
        )

    return generate


def _defer_generation_for_group_bot_admission(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
) -> bool:
    if not payload.group_bot_admission_state:
        return False
    from .group_bot_admission import evaluate_send_gate

    decision = evaluate_send_gate(
        session,
        tenant_id=action.tenant_id,
        group_id=int(payload.group_id or 0),
        account_id=int(action.account_id or 0),
        enforce=True,
        action_id=str(action.id),
    )
    if decision.allowed:
        _record_allowed_generation_admission(action, decision)
        session.commit()
        return False
    action.result = {
        **(action.result or {}),
        "error_code": decision.code or "group_bot_admission_wait",
        "validation_stage": "pre_ai_group_bot_admission",
        "group_bot_admission_state": decision.state,
        "group_bot_admission_id": decision.admission_id,
    }
    action.scheduled_at = _now() + timedelta(
        seconds=GROUP_BOT_ADMISSION_RETRY_SECONDS,
    )
    session.commit()
    return True


def _record_allowed_generation_admission(action: Action, decision) -> None:
    payload = dict(action.payload or {})
    payload.update({
        "group_bot_admission_id": decision.admission_id,
        "group_bot_admission_state": decision.state,
        "admission_version": decision.admission_version,
    })
    if decision.code == "post_follow_visibility_probe":
        payload["group_bot_post_follow_visibility_probe"] = True
    action.payload = payload
    result = dict(action.result or {})
    if result.get("validation_stage") == "pre_ai_group_bot_admission":
        for key in (
            "error_code",
            "validation_stage",
            "group_bot_admission_state",
            "group_bot_admission_id",
        ):
            result.pop(key, None)
        action.result = result


__all__ = ["drain_ai_generation"]
