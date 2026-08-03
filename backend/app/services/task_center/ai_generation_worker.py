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
from .ai_generation_runtime_config import (
    _content_obligation_fallback_ready,
    _due_catch_up_required,
    tenant_fallback_flags,
)
from .ai_generation_timing import GENERATION_LEASE, GENERATION_LOOKAHEAD
from .direct_check_in import is_due_catch_up_check_in
from .payloads import SendMessagePayload


GENERATABLE_STATUSES = ("pending", "ai_result_persist_unknown")
GROUP_BOT_ADMISSION_RETRY_SECONDS = 30
DUE_CATCH_UP_MAX_PIPELINE_DEPTH = 4
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
        blocked_group_ids: set[int] = set()
        while True:
            first = session.scalar(_claim_statement(
                session,
                _generation_filters(
                    excluded_action_ids=excluded_action_ids,
                    excluded_group_ids=blocked_group_ids,
                ),
            ).limit(1))
            if first is None:
                return None
            _lock_generation_group(session, first)
            if _generation_group_has_capacity(session, first):
                token = str(uuid4())
                _mark_generation_claim(first, owner, token)
                session.commit()
                return first.id, token, 1
            blocked_group_ids.add(_action_group_id(first))
            session.rollback()


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


def _generation_group_has_capacity(session: Session, action: Action) -> bool:
    occupied = aliased(Action)
    occupied_status = occupied.payload["ai_generation_status"].as_string()
    occupied_text = occupied.payload["message_text"].as_string()
    group_id = int((action.payload or {}).get("group_id") or 0)
    occupants = list(session.scalars(select(occupied).where(
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
    )))
    if not occupants:
        return True
    depth = _due_catch_up_pipeline_depth(session, action)
    return bool(
        depth > 1
        and len(occupants) < depth
        and all(_is_ready_due_catch_up(item) for item in occupants)
    )


def _due_catch_up_pipeline_depth(session: Session, action: Action) -> int:
    task = session.get(Task, action.task_id)
    if task is None:
        return 1
    config = dict(task.type_config or {})
    depth = int(config.get("due_catch_up_pipeline_depth") or 1)
    if depth < 1 or depth > DUE_CATCH_UP_MAX_PIPELINE_DEPTH:
        raise RuntimeError("due_catch_up_pipeline_depth must be between 1 and 4")
    payload = SendMessagePayload.model_validate(action.payload or {})
    eligible = bool(
        depth > 1
        and not str(config.get("ai_model") or "").strip()
        and tenant_fallback_flags(task)["_ai_group_static_fallback_enabled"]
        and not payload.reply_to_message_id
        and not payload.material_intent.strip()
        and action.primary_quantity_slot_id
        and _content_obligation_fallback_ready(session, action)
        and _due_catch_up_required(session, action, payload)
    )
    return depth if eligible else 1


def _is_ready_due_catch_up(action: Action) -> bool:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return bool(
        action.status in {"pending", "claiming", "executing"}
        and payload.get("ai_generation_status") == "ready"
        and str(payload.get("message_text") or "").strip()
        and is_due_catch_up_check_in(payload)
    )


def _action_group_id(action: Action) -> int:
    return int((action.payload or {}).get("group_id") or 0)


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


def _generation_filters(
    *,
    excluded_action_ids: set[str],
    excluded_group_ids: set[int],
) -> tuple:
    payload_status = Action.payload["ai_generation_status"].as_string()
    message_text = Action.payload["message_text"].as_string()
    executing = aliased(Action)
    account_is_free = ~select(executing.id).where(
        executing.account_id == Action.account_id,
        executing.status == "executing",
    ).exists()
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
    )
    if excluded_action_ids:
        filters = (*filters, Action.id.not_in(excluded_action_ids))
    if excluded_group_ids:
        filters = (
            *filters,
            Action.payload["group_id"].as_integer().not_in(excluded_group_ids),
        )
    return filters


def _mark_generation_claim(action: Action, owner: str, token: str) -> None:
    payload = dict(action.payload) if isinstance(action.payload, dict) else {}
    payload["ai_generation_status"] = "generating"
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
    if (
        payload.get("ai_generation_status") == "generating"
        and not str(payload.get("message_text") or "").strip()
    ):
        payload["ai_generation_status"] = "pending"
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
