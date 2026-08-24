from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models import Action, Task, TgAccount, TgGroup
from app.services._common import _now
from app.services.developer_apps import credentials_for_account

from .ai_generation_composition import PRODUCTION_GENERATION_DEPENDENCIES
from .ai_generation_admission_gate import defer_generation_for_group_bot_admission
from .ai_generation_claim_lifecycle import (
    defer_unprepared_batch,
    mark_generation_claim,
    owns_generation_claim,
    persisted_generation_outcome,
    release_prepared_batch,
    release_unprepared_batch,
)
from .ai_generation_dependencies import GenerationDependencies
from .ai_generation_dispatch import ensure_send_message_content
from .ai_generator import AiGenerationUnavailable, ProviderRouteDeferred
from .ai_generation_worker_types import GenerationOutcome, SequentialClaim
from .provider_admission import (
    ProviderAdmissionBlocked,
    ProviderAdmissionUnavailable,
    ensure_claim_admission,
)
from .ai_generation_runtime_config import (
    _content_obligation_fallback_ready,
    _due_catch_up_required,
    tenant_fallback_flags,
)
from .ai_quality_stats import record_provider_admission_unavailable
from .ai_generation_timing import GENERATION_LOOKAHEAD
from .direct_check_in import is_due_catch_up_check_in
from .payloads import SendMessagePayload


GENERATABLE_STATUSES = ("pending", "ai_result_persist_unknown")
DUE_CATCH_UP_MAX_PIPELINE_DEPTH = 4
GenerateAction = Callable[[Session, Action, TgAccount], None]

logger = logging.getLogger(__name__)


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
    try:
        processed = _drain_parallel_generation(
            session_factory, owner, max(1, int(limit)), processor
        )
    except ProviderAdmissionUnavailable as exc:
        logger.warning("ai generation claim stopped: %s", exc)
        return processed
    visited_action_ids: set[str] = set()
    while processed < max(1, int(limit)):
        try:
            claim = _claim_generation_batch(
                session_factory,
                owner,
                excluded_action_ids=visited_action_ids,
            )
        except ProviderAdmissionUnavailable as exc:
            logger.warning("ai generation claim stopped: %s", exc)
            return processed
        if claim is None:
            break
        visited_action_ids.add(claim.action_id)
        released = _process_sequential_claim(session_factory, processor, claim)
        if released is None:
            return processed
        processed += released
    return processed


def _process_sequential_claim(
    session_factory,
    processor: GenerateAction,
    claim: SequentialClaim,
) -> int | None:
    generation_failure: AiGenerationUnavailable | None = None
    provider_deferred: ProviderRouteDeferred | None = None
    admission_deferred = False
    with session_factory() as session:
        action = session.get(Action, claim.action_id)
        if not owns_generation_claim(action, claim.owner, claim.token):
            raise RuntimeError(f"AI generation claim lost for action {claim.action_id}")
        account = session.get(TgAccount, action.account_id)
        if account is None:
            raise RuntimeError(f"AI generation action {action.id} has no account")
        try:
            processor(session, action, account)
        except GenerationAdmissionDeferred:
            session.rollback()
            admission_deferred = True
        except ProviderAdmissionBlocked as exc:
            session.rollback()
            admission_deferred = True
            logger.info("ai generation deferred by provider admission: %s", exc)
        except ProviderAdmissionUnavailable as exc:
            session.rollback()
            _stop_sequential_claim(session_factory, claim, exc)
            return None
        except ProviderRouteDeferred as exc:
            session.rollback()
            provider_deferred = exc
        except AiGenerationUnavailable as exc:
            session.rollback()
            generation_failure = exc
    outcome = GenerationOutcome(generation_failure, admission_deferred, provider_deferred)
    if outcome.provider_deferred is not None:
        return _defer_sequential_claim(session_factory, claim, outcome.provider_deferred)
    if outcome.admission_deferred:
        release_unprepared_batch(session_factory, claim.owner, claim.token)
        return claim.claimed_count
    if outcome.failure is not None:
        persisted = persisted_generation_outcome(session_factory, claim.action_id)
        if not persisted:
            raise outcome.failure
        if persisted == "deferred":
            return claim.claimed_count
        release_unprepared_batch(session_factory, claim.owner, claim.token)
        return claim.claimed_count
    return release_prepared_batch(session_factory, claim.owner, claim.token)


def _stop_sequential_claim(
    session_factory,
    claim: SequentialClaim,
    error: ProviderAdmissionUnavailable,
) -> None:
    release_unprepared_batch(
        session_factory,
        claim.owner,
        claim.token,
        provider_admission_unavailable=True,
    )
    logger.warning("ai generation claim stopped: %s", error)


def _defer_sequential_claim(
    session_factory,
    claim: SequentialClaim,
    deferred: ProviderRouteDeferred,
) -> int:
    next_retry_at = _now() + timedelta(seconds=deferred.retry_after_seconds)
    defer_unprepared_batch(
        session_factory,
        claim.owner,
        claim.token,
        next_retry_at=next_retry_at,
    )
    return claim.claimed_count


def _drain_parallel_generation(
    session_factory,
    owner: str,
    limit: int,
    processor: GenerateAction,
) -> int:
    from .ai_generation_parallel import claim_parallel_generation

    claims = claim_parallel_generation(
        session_factory,
        owner=owner,
        limit=limit,
    )
    if not claims:
        return 0
    with ThreadPoolExecutor(max_workers=len(claims)) as executor:
        results = list(executor.map(
            lambda claim: _process_parallel_claim(
                session_factory, processor, claim
            ),
            claims,
        ))
    return sum(results)


def _process_parallel_claim(session_factory, processor, claim) -> int:
    from .ai_generation_parallel import finish_generation_job

    failure: AiGenerationUnavailable | None = None
    provider_deferred: ProviderRouteDeferred | None = None
    deferred = False
    with session_factory() as session:
        action = session.get(Action, claim.action_id)
        if not owns_generation_claim(action, claim.owner, claim.token):
            raise RuntimeError(f"AI generation claim lost for action {claim.action_id}")
        if not _generation_action_lifecycle_current(session, action):
            _cancel_stale_generation_action(session, action)
            finish_generation_job(session_factory, claim, state="cancelled")
            return 1
        account = session.get(TgAccount, action.account_id)
        if account is None:
            raise RuntimeError(f"AI generation action {action.id} has no account")
        try:
            processor(session, action, account)
        except GenerationAdmissionDeferred:
            session.rollback()
            deferred = True
        except ProviderAdmissionBlocked as exc:
            session.rollback()
            deferred = True
            logger.info("ai generation deferred by provider admission: %s", exc)
        except ProviderRouteDeferred as exc:
            session.rollback()
            provider_deferred = exc
        except AiGenerationUnavailable as exc:
            session.rollback()
            failure = exc
        except ProviderAdmissionUnavailable as exc:
            session.rollback()
            release_unprepared_batch(
                session_factory,
                claim.owner,
                claim.token,
                provider_admission_unavailable=True,
            )
            finish_generation_job(session_factory, claim, state="pending")
            raise
    outcome = GenerationOutcome(failure, deferred, provider_deferred)
    return _settle_parallel_outcome(session_factory, claim, outcome)


def _settle_parallel_outcome(session_factory, claim, outcome: GenerationOutcome) -> int:
    from .ai_generation_parallel import defer_parallel_generation, finish_generation_job

    if outcome.provider_deferred is not None:
        next_retry_at = _now() + timedelta(seconds=outcome.provider_deferred.retry_after_seconds)
        defer_parallel_generation(
            session_factory,
            claim,
            next_retry_at=next_retry_at,
        )
        return 1
    if outcome.admission_deferred:
        release_unprepared_batch(session_factory, claim.owner, claim.token)
        finish_generation_job(session_factory, claim, state="pending")
        return 1
    if outcome.failure is not None:
        persisted = persisted_generation_outcome(session_factory, claim.action_id)
        if not persisted:
            raise outcome.failure
        if persisted == "deferred":
            return 1
        release_unprepared_batch(session_factory, claim.owner, claim.token)
        finish_generation_job(session_factory, claim, state="failed")
        return 1
    released = release_prepared_batch(session_factory, claim.owner, claim.token)
    finish_generation_job(session_factory, claim, state="ready")
    return released


def _generation_action_lifecycle_current(
    session: Session,
    action: Action,
) -> bool:
    task = session.get(Task, action.task_id)
    return bool(
        task
        and task.status == "running"
        and task.deleted_at is None
        and int(action.task_lifecycle_epoch or 1)
        == int(task.task_lifecycle_epoch or 1)
    )


def _cancel_stale_generation_action(session: Session, action: Action) -> None:
    action.status = "skipped"
    action.result = {
        **dict(action.result or {}),
        "success": False,
        "error_code": "cancelled_by_task_lifecycle",
    }
    action.claim_owner = ""
    action.claim_token = ""
    action.lease_owner = ""
    action.lease_expires_at = None
    action.executed_at = _now()
    session.commit()


def _claim_generation_batch(
    session_factory,
    owner: str,
    *,
    excluded_action_ids: set[str],
) -> SequentialClaim | None:
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
            try:
                ensure_claim_admission(session)
            except ProviderAdmissionBlocked:
                session.rollback()
                return None
            except ProviderAdmissionUnavailable:
                record_provider_admission_unavailable(session, first)
                session.commit()
                raise
            _lock_generation_group(session, first)
            if _generation_group_has_capacity(session, first):
                token = str(uuid4())
                mark_generation_claim(first, owner, token)
                session.commit()
                return SequentialClaim(first.id, owner, token, 1)
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
        and not config.get("ai_two_stage_enabled")
        and not config.get("ai_content_route_v2_enabled")
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
        Task.fulfillment_contract_version != "fact_first_v3",
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


def _production_generate_action(
    dependencies: GenerationDependencies,
) -> GenerateAction:
    def generate(session: Session, action: Action, account: TgAccount) -> None:
        payload = SendMessagePayload.model_validate(action.payload or {})
        if defer_generation_for_group_bot_admission(
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


__all__ = ["drain_ai_generation"]
