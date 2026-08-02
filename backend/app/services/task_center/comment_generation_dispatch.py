from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, attributes

from app.models import (
    Action,
    ChannelMessage,
    ChannelMessageComment,
    Task,
)
from app.services._common import _now

from .ai_generator import AiGenerationUnavailable
from .ai_generation_state import GenerationAttemptStale, mark_attempt_outcome
from .channel_payloads import PostCommentPayload
from .comment_generation_pipeline import (
    CommentGenerationBlocked,
    CommentGenerationDependencies,
    CommentGenerationRequest,
    GeneratedCommentResult,
    generate_comment_result,
)
from .comment_generation_result import (
    evaluate_legacy_generated_comment,
    generated_comment_decision,
)
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
    try:
        if not request.cached_content:
            _mark_provider_call_started(session, request)
        generated = generate_comment_result(
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
        )
        raise AiGenerationUnavailable(exc.code) from exc
    except Exception as exc:
        session.rollback()
        _persist_generation_failure(session, request, str(exc))
        raise AiGenerationUnavailable("generation_failed") from exc
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
    refreshed = session.get(Action, action.id)
    if refreshed.status == "failed":
        _release_runtime_resources(refreshed)
        status = str((refreshed.payload or {}).get("ai_generation_status") or "generation_failed")
        raise AiGenerationUnavailable(status)
    return PostCommentPayload.model_validate(refreshed.payload or {})


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
    if target:
        session.commit()
        return
    _fail_generation_context(
        session,
        request,
        code="reply_target_missing",
        detail="引用评论已删除或不可访问",
    )


def _fail_generation_context(
    session: Session,
    request: CommentGenerationRequest,
    *,
    code: str,
    detail: str,
) -> None:
    current = _load_attempt_action(session, request)
    _fail_before_generation(current, code, detail)
    _cas_write_action(session, request, current)
    session.commit()
    _release_runtime_resources(current)
    raise AiGenerationUnavailable(code)


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


def _mark_provider_call_started(session: Session, request: CommentGenerationRequest) -> None:
    action = _load_attempt_action(session, request)
    action.result = {
        **(action.result or {}),
        "generation_stage": "provider_call_started",
        "ai_provider_call_started_at": _now().isoformat(),
        "ai_provider_call_attempt_id": request.attempt_id,
    }
    _cas_write_action(session, request, action)
    session.commit()


def _reply_target(payload: PostCommentPayload) -> dict:
    return {
        "message_id": int(payload.reply_to_message_id or 0),
        "author": payload.reply_target_author,
        "preview": payload.reply_target_preview,
        "source": payload.reply_target_source,
    }


def persist_comment_generation_result(
    session: Session,
    request: CommentGenerationRequest,
    generated: GeneratedCommentResult | str,
    *,
    tokens: int = 0,
) -> None:
    action = _load_attempt_action(session, request)
    result = (
        generated
        if isinstance(generated, GeneratedCommentResult)
        else evaluate_legacy_generated_comment(
            session,
            action,
            payload=request.payload,
            content=generated,
            tokens=tokens,
        )
    )
    decision = generated_comment_decision(result)
    if not decision.allowed:
        _fail_before_generation(
            action,
            decision.code,
            decision.detail,
            stage="ai_generation_quality",
        )
        action.result = {**(action.result or {}), "comment_quality_audit": decision.audit or {}}
        _cas_write_action(session, request, action)
        return
    data = dict(action.payload or {})
    data.update({
        "comment_text": result.content,
        "ai_generation_status": "ready",
        "ai_generation_tokens": max(0, int(result.tokens or 0)),
        "comment_generation_attempts": list(result.attempts),
        "comment_fallback_kind": result.fallback_kind,
        "content_source": (
            "comment_emoji_fallback"
            if result.fallback_kind == "emoji_text"
            else "normal"
        ),
        "quality_fallback": (
            "comment_emoji_fallback"
            if result.fallback_kind == "emoji_text"
            else ""
        ),
        "fallback_reason": result.fallback_reason,
        "planned_normal_text_emoji": (
            "no" if result.fallback_kind == "emoji_text" else "unresolved"
        ),
        "ai_generation_result_cache": {},
    })
    mark_attempt_outcome(data, request.attempt_id, "ready", timestamp=_now())
    action.payload = data
    action.result = {
        **(action.result or {}),
        "generation_stage": "generation_ready",
        "generation_outcome": "ready",
        "ai_generation_attempt_id": request.attempt_id,
        "comment_quality_audit": result.quality_audit or {},
    }
    _cas_write_action(session, request, action)


def _persist_generation_failure(
    session: Session,
    request: CommentGenerationRequest,
    detail: str,
    *,
    code: str = "generation_failed",
) -> None:
    action = _load_attempt_action(session, request)
    _fail_before_generation(action, code, detail or "AI 评论生成失败")
    _cas_write_action(session, request, action)
    session.commit()
    _release_runtime_resources(action)


def _persist_generation_unknown(
    session: Session,
    request: CommentGenerationRequest,
    generated: GeneratedCommentResult,
    *,
    detail: str,
) -> None:
    action = _load_attempt_action(session, request)
    data = dict(action.payload or {})
    data.update({
        "ai_generation_status": "ai_result_persist_unknown",
        "ai_generation_result_cache": {
            "content": generated.content,
            "tokens": max(0, int(generated.tokens or 0)),
            "fallback_kind": generated.fallback_kind,
            "fallback_reason": generated.fallback_reason,
            "attempts": list(generated.attempts),
            "attempt_id": request.attempt_id,
        },
    })
    mark_attempt_outcome(data, request.attempt_id, "ai_result_persist_unknown", timestamp=_now())
    action.payload = data
    action.status = "pending"
    action.lease_owner = ""
    action.lease_expires_at = None
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.result = {
        **(action.result or {}),
        "success": False,
        "error_code": "ai_result_persist_unknown",
        "error_message": detail or "AI 结果落库状态未知",
        "validation_stage": "ai_generation_persistence",
        "generation_stage": "ai_result_persist_unknown",
        "generation_outcome": "ai_result_persist_unknown",
    }
    _cas_write_action(session, request, action)
    session.commit()
    _release_runtime_resources(action)


def _fail_before_generation(
    action: Action,
    code: str,
    detail: str,
    *,
    stage: str = "ai_generation",
) -> None:
    data = dict(action.payload or {})
    data["ai_generation_status"] = code
    attempt_id = str(data.get("ai_generation_attempt_id") or "")
    if attempt_id:
        mark_attempt_outcome(data, attempt_id, code, timestamp=_now())
    action.payload = data
    action.status = "failed"
    action.executed_at = _now()
    action.lease_owner = ""
    action.lease_expires_at = None
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.result = {
        **(action.result or {}),
        "success": False,
        "error_code": code,
        "error_message": detail,
        "validation_stage": "ai_reply_target" if code.startswith("reply_target") else stage,
        "generation_stage": stage,
        "generation_outcome": code,
    }


def _load_attempt_action(session: Session, request: CommentGenerationRequest) -> Action:
    action = session.scalar(select(Action).where(
        Action.id == request.action_id,
        Action.tenant_id == request.tenant_id,
        Action.task_id == request.task_id,
        Action.status == "executing",
        Action.payload["ai_generation_claim_owner"].as_string() == request.claim_owner,
        Action.payload["ai_generation_claim_token"].as_string() == request.claim_token,
        Action.payload["ai_generation_attempt_id"].as_string() == request.attempt_id,
    ))
    if not action:
        raise GenerationAttemptStale("ai_generation_attempt_stale")
    return action


def _cas_write_action(session: Session, request: CommentGenerationRequest, action: Action) -> None:
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
        "executed_at": action.executed_at,
        "claim_owner": action.claim_owner,
        "claim_token": action.claim_token,
        "claim_expires_at": action.claim_expires_at,
        "lease_owner": action.lease_owner,
        "lease_expires_at": action.lease_expires_at,
    }


__all__ = [
    "CommentGenerationDependencies",
    "GenerationAttemptStale",
    "PRODUCTION_COMMENT_GENERATION_DEPENDENCIES",
    "ensure_post_comment_content",
    "persist_comment_generation_result",
    "prepare_comment_generation_request",
]
