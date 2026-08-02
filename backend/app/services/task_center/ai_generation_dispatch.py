from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, GroupContextMessage, Task, TgAccount
from app.services._common import _now

from .ai_generation_dependencies import GenerationDependencies
from .ai_generation_commit import commit_generation_action, load_generation_batch
from .ai_generation_persistence import persist_generation_results as _persist_generation_results
from .ai_generation_state import (
    GenerationAttemptStale,
    GenerationMappingError,
    begin_generation_attempt,
    cached_generation_result,
)
from .ai_generation_recovery import persist_generation_unknown
from .ai_generation_pipeline import SlotGenerationResult, generate_quality_results
from .ai_generation_slots import generation_slot as _generation_slot
from .ai_generation_slots import reply_targets as _reply_targets
from .ai_generator import AI_GENERATION_UNAVAILABLE_MESSAGE, AiGenerationUnavailable
from .ai_generation_quality import fail_generation_action, fail_generation_batch
from .group_ai_prompt_scope import rebuild_group_prompt_inputs
from .group_ai_scope import REMOTE_REPLY_TARGET_OBSERVATION
from .ai_generation_guards import (
    invalidate_superseded_normal_generation as _invalidate_superseded_normal_generation,
    latest_context_rows as _latest_context_rows,
    prepare_generation_guards as _prepare_generation_guards,
    ready_generation_payload as _ready_generation_payload,
    record_should_speak_shadow as _record_should_speak_shadow,
    requeue_normal_generation_after_context_change,
    require_normal_context_watermark as _require_normal_context_watermark,
    validate_local_reply_target as _validate_local_reply_target,
)
from .ai_generation_runtime_config import (
    _content_obligation_fallback_ready,
    _latest_safe_send_at,
    build_runtime_config as _build_runtime_config,
    payload_map as _payload_map,
    quality_snapshot as _quality_snapshot,
    tenant_fallback_flags as _tenant_fallback_flags,
)
from .ai_quality_stats import (
    clear_quality_blocker as _clear_quality_blocker,
    quality_scope_key as _quality_scope_key,
    record_quality_event as _record_quality_event,
)
from .payloads import SendMessagePayload


CONTEXT_HISTORY_MAX_CHARS = 1000


@dataclass(frozen=True)
class GenerationRequest:
    action_id: str
    tenant_id: int
    task_id: str
    group_id: int
    account_id: int
    session_ciphertext: str
    credentials: object
    peer_id: str
    is_reply: bool
    target_label: str
    history: str
    config: dict
    reply_targets: list[dict]
    batch_ids: list[str]
    attempt_id: str
    claim_owner: str
    claim_token: str
    cached_contents: list[str]
    cached_tokens: int
    duplicate_baseline_messages: list[str]
    quality_snapshots: list[dict]
    chat_mode: str
    context_message_ids: list[int]
    fact_anchor_required: bool
    low_confidence_silence_enabled: bool


def ensure_send_message_content(
    session: Session,
    action: Action,
    account: TgAccount,
    *,
    payload: SendMessagePayload,
    credentials=None,
    dependencies: GenerationDependencies,
    allow_provider_call: bool = True,
) -> SendMessagePayload:
    task = session.get(Task, action.task_id) if action.task_id else None
    if not task:
        raise AiGenerationUnavailable("AI 生成缺少任务配置")
    guarded = _prepare_generation_guards(
        session,
        task,
        action,
        account=account,
        payload=payload,
    )
    if guarded is not None:
        return guarded
    payload = _invalidate_superseded_normal_generation(
        session,
        task,
        action,
        payload=payload,
    )
    if payload.message_text.strip():
        return payload
    if payload.ai_generation_status not in {"pending", "ai_result_persist_unknown"}:
        raise AiGenerationUnavailable("send_message action 缺少可发送文案")
    if not allow_provider_call:
        if (action.result or {}).get("generation_stage") == "context_superseded":
            requeue_normal_generation_after_context_change(
                session,
                task,
                action,
                payload=payload,
            )
        raise AiGenerationUnavailable("ai_generation_not_ready")
    return _generate_normal_content(
        session,
        task,
        action,
        account=account,
        payload=payload,
        credentials=credentials,
        dependencies=dependencies,
    )


def _generate_normal_content(
    session: Session,
    task: Task,
    action: Action,
    *,
    account: TgAccount,
    payload: SendMessagePayload,
    credentials,
    dependencies: GenerationDependencies,
) -> SendMessagePayload:
    batch = _refresh_normal_context(session, task, _pending_generation_batch(session, action, payload))
    batch = rebuild_group_prompt_inputs(session, task, batch)
    request = _prepare_generation_request(
        session,
        task,
        batch,
        account=account,
        credentials=credentials,
    )
    results, tokens = _generate_request_results(session, request, dependencies)
    _commit_generation_results(session, request, results, tokens=tokens)
    return _ready_generation_payload(session, action)


def _generate_request_results(
    session: Session,
    request: GenerationRequest,
    dependencies: GenerationDependencies,
) -> tuple[list[SlotGenerationResult], int]:
    try:
        return _generate_without_transaction(session, request, dependencies)
    except GenerationMappingError as exc:
        fail_generation_batch(session, request, str(exc), detail=str(exc), mapping_error=exc)
        session.commit()
        raise AiGenerationUnavailable(str(exc)) from exc
    except GenerationAttemptStale:
        session.rollback()
        raise


def _commit_generation_results(
    session: Session,
    request: GenerationRequest,
    results: list[SlotGenerationResult],
    *,
    tokens: int,
) -> None:
    try:
        _persist_generation_results(session, request, results, tokens=tokens)
        session.commit()
    except GenerationMappingError as exc:
        session.rollback()
        fail_generation_batch(
            session,
            request,
            str(exc),
            detail=str(exc),
            mapping_error=exc,
        )
        session.commit()
        raise AiGenerationUnavailable(str(exc)) from exc
    except GenerationAttemptStale:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        contents = [result.content for result in results]
        persist_generation_unknown(
            session,
            request,
            contents,
            tokens=tokens,
            attempt_id=request.attempt_id,
        )
        session.commit()
        raise AiGenerationUnavailable("ai_result_persist_unknown")


def _prepare_generation_request(
    session: Session,
    task: Task,
    batch: list[tuple[Action, SendMessagePayload]],
    *,
    account: TgAccount,
    credentials,
) -> GenerationRequest:
    action, payload = batch[0]
    peer_id = _validate_local_reply_target(
        session,
        action,
        payload=payload,
        account_id=account.id,
    )
    cached = [cached_generation_result(item) for _row, item in batch]
    cached_contents = [item[0] for item in cached if item]
    cached_tokens = sum(item[1] for item in cached if item)
    attempt_id = payload.ai_generation_attempt_id
    if len(cached_contents) != len(batch):
        attempt_id, _request_id = begin_generation_attempt(batch, _now())
        cached_contents = []
        cached_tokens = 0
    request = _generation_request(
        task,
        batch,
        account,
        session=session,
        credentials=credentials,
        peer_id=peer_id,
        attempt_id=attempt_id,
    )
    session.commit()
    return GenerationRequest(
        **{**request.__dict__, "cached_contents": cached_contents, "cached_tokens": cached_tokens},
    )


def _generation_request(
    task: Task,
    batch: list[tuple[Action, SendMessagePayload]],
    account: TgAccount,
    *,
    session: Session,
    credentials,
    peer_id: str,
    attempt_id: str,
) -> GenerationRequest:
    action, payload = batch[0]
    return GenerationRequest(
        action_id=action.id,
        tenant_id=action.tenant_id,
        task_id=str(action.task_id or ""),
        group_id=int(payload.group_id or 0),
        account_id=account.id,
        session_ciphertext=account.session_ciphertext,
        credentials=credentials,
        peer_id=peer_id,
        is_reply=bool(payload.reply_to_message_id),
        target_label=payload.target_display,
        history=payload.ai_generation_history,
        config={
            **_runtime_config(session, task, batch),
            **_tenant_fallback_flags(task),
            "_close_db_transaction_before_ai": True,
        },
        reply_targets=_reply_targets(batch),
        batch_ids=[row.id for row, _item in batch],
        attempt_id=attempt_id,
        claim_owner=payload.ai_generation_claim_owner,
        claim_token=payload.ai_generation_claim_token,
        cached_contents=[],
        cached_tokens=0,
        duplicate_baseline_messages=_duplicate_baseline_messages(
            session,
            batch,
            payload=payload,
        ),
        quality_snapshots=[_quality_snapshot(item) for _row, item in batch],
        chat_mode=payload.chat_mode,
        context_message_ids=list(payload.context_message_ids),
        fact_anchor_required=bool((task.type_config or {}).get("fact_anchor_required", True)),
        low_confidence_silence_enabled=bool((task.type_config or {}).get("low_confidence_silence_enabled", True)),
    )


def _duplicate_baseline_messages(
    session: Session,
    batch: list[tuple[Action, SendMessagePayload]],
    *,
    payload: SendMessagePayload,
) -> list[str]:
    baseline = [
        line
        for line in payload.ai_generation_history.splitlines()
        if line.strip()
    ]
    batch_ids = {action.id for action, _item in batch}
    account_id = batch[0][0].account_id
    candidates = session.scalars(select(Action).where(
        Action.tenant_id == batch[0][0].tenant_id,
        Action.task_id == batch[0][0].task_id,
        Action.account_id == account_id,
        Action.action_type == "send_message",
        Action.status.in_(("pending", "executing")),
        Action.id.not_in(batch_ids),
    ))
    for candidate in candidates:
        data = candidate.payload if isinstance(candidate.payload, dict) else {}
        if int(data.get("group_id") or 0) != int(payload.group_id or 0):
            continue
        if text := str(data.get("message_text") or "").strip():
            baseline.append(text)
    return baseline


def _generate_without_transaction(
    session: Session,
    request: GenerationRequest,
    dependencies: GenerationDependencies,
) -> tuple[list[SlotGenerationResult], int]:
    if request.is_reply:
        _validate_remote_reply_target(session, request, dependencies)
    if not request.cached_contents:
        _mark_provider_call_started(session, request)
    try:
        return generate_quality_results(session, request, dependencies)
    except AiGenerationUnavailable as exc:
        code = str(exc) or AI_GENERATION_UNAVAILABLE_MESSAGE
        fail_generation_batch(
            session,
            request,
            _generation_failure_code(code),
            detail=code,
        )
        session.commit()
        raise


def _generation_failure_code(code: str) -> str:
    if code in {
        "ai_generation_deadline_budget_exhausted",
        "ai_generation_deadline_invalid",
    }:
        return code
    return "ai_generation_failed"


def _mark_provider_call_started(session: Session, request: GenerationRequest) -> None:
    timestamp = _now().isoformat()
    for action, _payload in load_generation_batch(session, request):
        action.result = {
            **(action.result or {}),
            "generation_stage": "provider_call_started",
            "ai_provider_call_started_at": timestamp,
        }
        commit_generation_action(session, request, action)
    session.commit()


def _validate_remote_reply_target(
    session: Session,
    request: GenerationRequest,
    dependencies: GenerationDependencies,
) -> None:
    probe = dependencies.reply_target_probe(
        request.account_id,
        request.peer_id,
        "group",
        request.session_ciphertext,
        request.credentials,
    )
    snapshot = None
    target_id = str(request.reply_targets[0]["message_id"])
    if probe.ok:
        snapshot = dependencies.reply_message_fetcher(
            request.account_id,
            request.peer_id,
            target_id,
            request.session_ciphertext,
            request.credentials,
        )
    if probe.ok and snapshot and str(snapshot.remote_message_id) == target_id:
        return
    action = session.get(Action, request.action_id)
    fail_generation_action(
        action,
        "reply_target_missing",
        probe.detail or "远端引用目标不存在或不可访问",
        stage="ai_reply_target",
    )
    action.result = {
        **(action.result or {}),
        "reply_target_observation": REMOTE_REPLY_TARGET_OBSERVATION,
        "reply_target_message_id": target_id,
        "reply_target_probe_detail": str(probe.detail or ""),
    }
    session.commit()
    raise AiGenerationUnavailable("reply_target_missing")


def _pending_generation_batch(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
) -> list[tuple[Action, SendMessagePayload]]:
    del session
    return [(action, payload)]


def _refresh_normal_context(
    session: Session,
    task: Task,
    batch: list[tuple[Action, SendMessagePayload]],
) -> list[tuple[Action, SendMessagePayload]]:
    if not batch or batch[0][1].reply_to_message_id:
        return batch
    rows = _latest_context_rows(session, batch[0][1], task)
    if not rows:
        return batch
    history = "\n".join(f"{row.sender_name}: {row.content}" for row in rows)[-CONTEXT_HISTORY_MAX_CHARS:]
    context_ids = [int(row.id) for row in rows]
    refreshed = []
    for action, payload in batch:
        updated = payload.model_copy(update={
            "anchor_message_ids": context_ids,
            "context_message_ids": context_ids,
            "context_snapshot_message_id": max(context_ids),
            "ai_generation_history": history,
            "ai_generation_context_count": len(context_ids),
        })
        action.payload = updated.model_dump(mode="json")
        refreshed.append((action, updated))
    return refreshed


def _runtime_config(
    session: Session,
    task: Task,
    batch: list[tuple[Action, SendMessagePayload]],
) -> dict:
    return _build_runtime_config(
        session,
        task,
        batch,
        generation_slot_builder=_generation_slot,
    )


__all__ = ["GenerationAttemptStale", "ensure_send_message_content"]
