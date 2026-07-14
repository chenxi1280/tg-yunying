from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, attributes

from app.models import Action, ChannelMessage, ChannelMessageComment, OperationTarget, Task, TgGroup
from app.services._common import _now
from app.services.content_filters import ContentFilterResult, filter_outbound_content

from .ai_generator import (
    AiGenerationUnavailable,
    clean_channel_comment_contents,
    generate_channel_comments,
    generate_channel_reply_comments,
)
from .ai_generation_state import GenerationAttemptStale, mark_attempt_outcome
from .channel_payloads import PostCommentPayload
from .runtime_resources import _release_runtime_resources


def _commit_session(session: Session) -> None:
    session.commit()


@dataclass(frozen=True)
class CommentGenerationDependencies:
    direct_generator: Callable = generate_channel_comments
    reply_generator: Callable = generate_channel_reply_comments
    phase_c_commit: Callable[[Session], None] = _commit_session


@dataclass(frozen=True)
class CommentGenerationRequest:
    action_id: str
    tenant_id: int
    task_id: str
    account_id: int
    payload: PostCommentPayload
    config: dict
    attempt_id: str
    request_id: str
    claim_owner: str
    claim_token: str
    cached_content: str
    cached_tokens: int


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
        content, tokens = _generate_comment(session, request, dependencies)
    except GenerationAttemptStale:
        raise
    except Exception as exc:
        session.rollback()
        _persist_generation_failure(session, request, str(exc))
        raise AiGenerationUnavailable("generation_failed") from exc
    try:
        persist_comment_generation_result(session, request, content, tokens=tokens)
        dependencies.phase_c_commit(session)
    except GenerationAttemptStale:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        _persist_generation_unknown(session, request, content, tokens=tokens, detail=str(exc))
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
        config={**dict(task.type_config or {}), "_close_db_transaction_before_ai": True},
        attempt_id=attempt_id,
        request_id=request_id,
        claim_owner=str(data.get("ai_generation_claim_owner") or ""),
        claim_token=str(data.get("ai_generation_claim_token") or ""),
        cached_content=str(cached.get("content") or "").strip(),
        cached_tokens=int(cached.get("tokens") or 0),
    )
    session.commit()
    _validate_reply_target(session, action, task, request=request)
    return request


def _validate_generation_claim(action: Action, payload: PostCommentPayload) -> None:
    if action.status != "executing":
        raise GenerationAttemptStale("ai_generation_attempt_stale")
    if not payload.ai_generation_claim_owner or not payload.ai_generation_claim_token:
        raise GenerationAttemptStale("ai_generation_attempt_stale")


def _validate_reply_target(
    session: Session,
    action: Action,
    task: Task,
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
    message = session.scalar(select(ChannelMessage.id).where(
        ChannelMessage.id == payload.channel_message_id,
        ChannelMessage.tenant_id == action.tenant_id,
        ChannelMessage.comment_available.is_(True),
    ))
    window = int((task.type_config or {}).get("context_bound_schedule_window_seconds") or 300)
    stale = (_naive(_now()) - _naive(action.created_at)).total_seconds() > window
    if target and message and not stale:
        session.commit()
        return
    code = "reply_target_stale" if stale else "reply_target_missing"
    current = _load_attempt_action(session, request)
    _fail_before_generation(current, code, "引用评论已过期、删除或不可访问")
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
    action.result = {
        **(action.result or {}),
        "generation_stage": "generation_claimed",
        "generation_outcome": "in_progress",
        "ai_generation_attempt_id": attempt_id,
    }


def _generate_comment(
    session: Session,
    request: CommentGenerationRequest,
    dependencies: CommentGenerationDependencies,
) -> tuple[str, int]:
    if request.cached_content:
        return request.cached_content, request.cached_tokens
    if session.in_transaction():
        raise RuntimeError("comment generation transaction boundary is open")
    payload = request.payload
    common = {
        "count": 1,
        "message_content": payload.message_content,
        "target_label": payload.target_display,
    }
    if payload.reply_to_message_id:
        contents, tokens = dependencies.reply_generator(
            session,
            request.tenant_id,
            request.config,
            reply_targets=[_reply_target(payload)],
            message_content=payload.message_content,
            target_label=payload.target_display,
        )
    else:
        contents, tokens = dependencies.direct_generator(
            session,
            request.tenant_id,
            request.config,
            **common,
        )
    cleaned = clean_channel_comment_contents(list(contents or []), limit=1)
    if len(cleaned) != 1:
        raise AiGenerationUnavailable("AI 评论候选质量不达标")
    return str(cleaned[0]).strip(), int(tokens or 0)


def _mark_provider_call_started(session: Session, request: CommentGenerationRequest) -> None:
    action = _load_attempt_action(session, request)
    action.result = {
        **(action.result or {}),
        "generation_stage": "provider_call_started",
        "ai_provider_call_started_at": _now().isoformat(),
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
    content: str,
    *,
    tokens: int,
) -> None:
    action = _load_attempt_action(session, request)
    filtered = _filter_comment_content(
        session,
        action,
        payload=request.payload,
        content=content,
    )
    if not filtered.ok:
        _fail_before_generation(action, "content_rejected", filtered.reason)
        _cas_write_action(session, request, action)
        return
    data = dict(action.payload or {})
    data.update({
        "comment_text": filtered.content,
        "ai_generation_status": "ready",
        "ai_generation_tokens": max(0, int(tokens or 0)),
        "ai_generation_result_cache": {},
    })
    mark_attempt_outcome(data, request.attempt_id, "ready", timestamp=_now())
    action.payload = data
    action.result = {
        **(action.result or {}),
        "generation_stage": "generation_ready",
        "generation_outcome": "ready",
        "ai_generation_attempt_id": request.attempt_id,
    }
    _cas_write_action(session, request, action)


def _filter_comment_content(
    session: Session,
    action: Action,
    *,
    payload: PostCommentPayload,
    content: str,
):
    channel = session.get(OperationTarget, int(payload.channel_target_id or 0))
    group = session.scalar(select(TgGroup).where(
        TgGroup.tenant_id == action.tenant_id,
        TgGroup.tg_peer_id == (channel.tg_peer_id if channel else ""),
    ))
    if not group:
        return ContentFilterResult(False, "", "频道评论缺少可校验的讨论组")
    return filter_outbound_content(
        session,
        tenant_id=action.tenant_id,
        group=group,
        content=content,
    )


def _persist_generation_failure(session: Session, request: CommentGenerationRequest, detail: str) -> None:
    action = _load_attempt_action(session, request)
    _fail_before_generation(action, "generation_failed", detail or "AI 评论生成失败")
    _cas_write_action(session, request, action)
    session.commit()
    _release_runtime_resources(action)


def _persist_generation_unknown(
    session: Session,
    request: CommentGenerationRequest,
    content: str,
    *,
    tokens: int,
    detail: str,
) -> None:
    action = _load_attempt_action(session, request)
    data = dict(action.payload or {})
    data.update({
        "ai_generation_status": "ai_result_persist_unknown",
        "ai_generation_result_cache": {
            "content": str(content or "").strip(),
            "tokens": max(0, int(tokens or 0)),
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


def _fail_before_generation(action: Action, code: str, detail: str) -> None:
    data = dict(action.payload or {})
    data["ai_generation_status"] = code
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
        "validation_stage": "ai_reply_target" if code.startswith("reply_target") else "ai_generation",
        "generation_stage": "ai_generation",
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


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


__all__ = [
    "CommentGenerationDependencies",
    "GenerationAttemptStale",
    "PRODUCTION_COMMENT_GENERATION_DEPENDENCIES",
    "ensure_post_comment_content",
    "persist_comment_generation_result",
    "prepare_comment_generation_request",
]
