from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelMessage,
    ChannelMessageComment,
    Task,
)
from app.services._common import _now

from .ai_generator import AiGenerationUnavailable
from .ai_generation_state import GenerationAttemptStale
from .channel_payloads import PostCommentPayload
from .comment_generation_job import (
    CommentGenerationJobConflict,
    claim_comment_generation_job,
)
from .comment_generation_pipeline import (
    CommentGenerationBlocked,
    CommentGenerationDependencies,
    CommentGenerationRequest,
    GeneratedCommentResult,
    generate_comment_result,
)
from .comment_generation_persistence import (
    fail_generation_context as _fail_generation_context,
    load_attempt_action as _load_attempt_action,
    mark_provider_call_started as _mark_provider_call_started,
    persist_comment_generation_result,
    persist_generation_failure as _persist_generation_failure,
    persist_generation_unknown as _persist_generation_unknown,
)
from .comment_reply_target_authority import has_authoritative_own_history_target
from .runtime_resources import _release_runtime_resources


PRODUCTION_COMMENT_GENERATION_DEPENDENCIES = CommentGenerationDependencies()


def ensure_post_comment_content(
    session: Session,
    action: Action,
    *,
    payload: PostCommentPayload,
    dependencies: CommentGenerationDependencies,
) -> PostCommentPayload:
    if payload.comment_text.strip() and payload.ai_generation_status in {"", "ready"}:
        ready = payload.model_copy(update={"ai_generation_status": "ready"})
        action.payload = ready.model_dump(mode="json")
        return ready
    if payload.ai_generation_status not in {"pending", "ai_result_persist_unknown"}:
        raise AiGenerationUnavailable("post_comment action 缺少可发送文案")
    task = session.get(Task, action.task_id) if action.task_id else None
    if not task:
        raise AiGenerationUnavailable("AI 评论生成缺少任务配置")
    request = prepare_comment_generation_request(session, action, task)
    generated = _generate_comment(session, request, dependencies)
    _persist_generated_comment(
        session, request, generated, dependencies=dependencies,
    )
    refreshed = session.get(Action, action.id)
    if refreshed.status == "failed":
        _release_runtime_resources(refreshed)
        status = str((refreshed.payload or {}).get("ai_generation_status") or "generation_failed")
        raise AiGenerationUnavailable(status)
    return PostCommentPayload.model_validate(refreshed.payload or {})


def _generate_comment(
    session: Session,
    request: CommentGenerationRequest,
    dependencies: CommentGenerationDependencies,
) -> GeneratedCommentResult:
    try:
        if not request.cached_content:
            _mark_provider_call_started(session, request)
        return generate_comment_result(
            session,
            request,
            dependencies,
            action_loader=_load_attempt_action,
        )
    except GenerationAttemptStale:
        session.rollback()
        raise
    except CommentGenerationBlocked as exc:
        session.rollback()
        _persist_generation_failure(
            session,
            request,
            exc.detail,
            code=exc.code,
            evaluator_evidence=exc.evaluator_evidence,
            tokens=exc.tokens,
        )
        raise AiGenerationUnavailable(exc.code) from exc
    except Exception as exc:
        session.rollback()
        _persist_generation_failure(session, request, str(exc))
        raise AiGenerationUnavailable("generation_failed") from exc


def _persist_generated_comment(
    session: Session,
    request: CommentGenerationRequest,
    generated: GeneratedCommentResult,
    *,
    dependencies: CommentGenerationDependencies,
) -> None:
    try:
        persist_comment_generation_result(session, request, generated)
        dependencies.phase_c_commit(session)
    except GenerationAttemptStale:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        _persist_generation_unknown(session, request, generated, detail=str(exc))
        raise AiGenerationUnavailable("ai_result_persist_unknown") from exc


def prepare_comment_generation_request(
    session: Session,
    action: Action,
    task: Task,
) -> CommentGenerationRequest:
    payload = PostCommentPayload.model_validate(action.payload or {})
    _validate_generation_claim(action, payload)
    attempt_id = str(uuid4())
    request_id = str(uuid4())
    data = payload.model_dump(mode="json")
    cached = dict(data.get("ai_generation_result_cache") or {})
    try:
        job = claim_comment_generation_job(
            session,
            action,
            payload,
            owner=str(data.get("ai_generation_claim_owner") or ""),
        )
    except CommentGenerationJobConflict as exc:
        session.rollback()
        raise GenerationAttemptStale("comment_generation_job_conflict") from exc
    data["generation_job_id"] = job.id
    _mark_generating(action, data, attempt_id=attempt_id, request_id=request_id)
    request = CommentGenerationRequest(
        action_id=action.id,
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        account_id=int(action.account_id or 0),
        payload=PostCommentPayload.model_validate(data),
        config=_generation_config(task, payload),
        attempt_id=attempt_id,
        request_id=request_id,
        claim_owner=str(data.get("ai_generation_claim_owner") or ""),
        claim_token=str(data.get("ai_generation_claim_token") or ""),
        cached_content=str(cached.get("content") or "").strip(),
        cached_tokens=int(cached.get("tokens") or 0),
        cached_fallback_kind=str(cached.get("fallback_kind") or ""),
        cached_fallback_reason=str(cached.get("fallback_reason") or ""),
        cached_attempts=tuple(cached.get("attempts") or ()),
    )
    session.commit()
    _validate_comment_target(session, action, request=request)
    _validate_reply_target(session, action, request=request)
    return request


def _generation_config(task: Task, payload: PostCommentPayload) -> dict:
    config = dict(task.type_config or {})
    config.pop("target_comment_profile", None)
    summary = str(payload.profile_hit_summary or "").strip()
    if summary:
        config["target_comment_profile"] = summary
    if payload.account_mask_summary:
        config["account_mask_summary"] = payload.account_mask_summary
    config["_close_db_transaction_before_ai"] = True
    return config


def _validate_generation_claim(action: Action, payload: PostCommentPayload) -> None:
    if action.status != "executing":
        raise GenerationAttemptStale("ai_generation_attempt_stale")
    if not payload.ai_generation_claim_owner or not payload.ai_generation_claim_token:
        raise GenerationAttemptStale("ai_generation_attempt_stale")


def _validate_comment_target(
    session: Session,
    action: Action,
    *,
    request: CommentGenerationRequest,
) -> None:
    payload = request.payload
    target = session.scalar(select(ChannelMessage.id).where(
        ChannelMessage.id == payload.channel_message_id,
        ChannelMessage.tenant_id == action.tenant_id,
        ChannelMessage.channel_target_id == payload.channel_target_id,
        ChannelMessage.message_id == payload.message_id,
        ChannelMessage.comment_available.is_(True),
    ))
    if target:
        session.commit()
        return
    _fail_generation_context(
        session,
        request,
        code="comment_unavailable_message",
        detail="频道源消息不存在或评论区已关闭",
    )


def _validate_reply_target(
    session: Session,
    action: Action,
    *,
    request: CommentGenerationRequest,
) -> None:
    payload = request.payload
    if not payload.reply_to_message_id:
        return
    target = session.scalar(select(ChannelMessageComment.id).where(
        ChannelMessageComment.tenant_id == action.tenant_id,
        ChannelMessageComment.channel_target_id == payload.channel_target_id,
        ChannelMessageComment.channel_message_id == payload.channel_message_id,
        ChannelMessageComment.comment_message_id == payload.reply_to_message_id,
    ))
    if target or has_authoritative_own_history_target(session, action, payload):
        session.commit()
        return
    _fail_generation_context(
        session,
        request,
        code="reply_target_missing",
        detail="引用评论已删除或不可访问",
    )


def _mark_generating(action: Action, data: dict, *, attempt_id: str, request_id: str) -> None:
    history = [dict(item) for item in list(data.get("ai_generation_attempt_history") or [])]
    history.append({
        "attempt_id": attempt_id,
        "request_id": request_id,
        "slot_id": str(data.get("slot_id") or ""),
        "lease_owner": action.lease_owner,
        "started_at": _now().isoformat(),
        "outcome": "in_progress",
    })
    data.update({
        "ai_generation_status": "generating",
        "ai_generation_attempt_id": attempt_id,
        "ai_generation_request_id": request_id,
        "ai_generation_attempt_history": history,
    })
    action.payload = data
    result = dict(action.result or {})
    result.pop("ai_provider_call_started_at", None)
    result.pop("ai_provider_call_attempt_id", None)
    action.result = {
        **result,
        "generation_stage": "generation_claimed",
        "generation_outcome": "in_progress",
        "ai_generation_attempt_id": attempt_id,
        "ai_generation_request_id": request_id,
    }


def _reply_target(payload: PostCommentPayload) -> dict:
    return {
        "message_id": int(payload.reply_to_message_id or 0),
        "author": payload.reply_target_author,
        "preview": payload.reply_target_preview,
        "source": payload.reply_target_source,
    }


__all__ = [
    "CommentGenerationDependencies",
    "GenerationAttemptStale",
    "PRODUCTION_COMMENT_GENERATION_DEPENDENCIES",
    "ensure_post_comment_content",
    "persist_comment_generation_result",
    "prepare_comment_generation_request",
]
