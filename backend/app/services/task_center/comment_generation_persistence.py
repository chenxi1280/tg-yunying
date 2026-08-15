from __future__ import annotations

import hashlib

from sqlalchemy import select, update
from sqlalchemy.orm import Session, attributes

from app.models import Action
from app.services._common import _now

from .ai_generation_state import GenerationAttemptStale, mark_attempt_outcome
from .ai_generator import AiGenerationUnavailable
from .comment_generation_job import finish_comment_generation_job
from .comment_generation_pipeline import CommentGenerationRequest, GeneratedCommentResult
from .comment_generation_result import (
    evaluate_legacy_generated_comment,
    generated_comment_decision,
)
from .runtime_resources import _release_runtime_resources


def fail_generation_context(
    session: Session,
    request: CommentGenerationRequest,
    *,
    code: str,
    detail: str,
) -> None:
    action = load_attempt_action(session, request)
    finish_comment_generation_job(
        session, action, request.payload, state="failed", owner=request.claim_owner,
    )
    _fail_before_generation(action, code, detail)
    _cas_write_action(session, request, action)
    session.commit()
    _release_runtime_resources(action)
    raise AiGenerationUnavailable(code)


def mark_provider_call_started(
    session: Session,
    request: CommentGenerationRequest,
) -> None:
    action = load_attempt_action(session, request)
    action.result = {
        **(action.result or {}),
        "generation_stage": "provider_call_started",
        "ai_provider_call_started_at": _now().isoformat(),
        "ai_provider_call_attempt_id": request.attempt_id,
    }
    _cas_write_action(session, request, action)
    session.commit()


def persist_comment_generation_result(
    session: Session,
    request: CommentGenerationRequest,
    generated: GeneratedCommentResult | str,
    *,
    tokens: int = 0,
) -> None:
    action = load_attempt_action(session, request)
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
        finish_comment_generation_job(
            session, action, request.payload, state="failed", owner=request.claim_owner,
        )
        _fail_before_generation(
            action, decision.code, decision.detail, stage="ai_generation_quality",
        )
        action.result = {
            **(action.result or {}),
            "comment_quality_audit": decision.audit or {},
        }
        _cas_write_action(session, request, action)
        return
    _apply_generation_ready(action, request, result)
    finish_comment_generation_job(
        session, action, request.payload, state="ready", owner=request.claim_owner,
    )
    _cas_write_action(session, request, action)


def _apply_generation_ready(
    action: Action,
    request: CommentGenerationRequest,
    result: GeneratedCommentResult,
) -> None:
    data = dict(action.payload or {})
    is_emoji_fallback = result.fallback_kind == "emoji_text"
    data.update({
        "comment_text": result.content,
        "ai_generation_status": "ready",
        "ai_generation_tokens": max(0, int(result.tokens or 0)),
        "comment_generation_attempts": list(result.attempts),
        "comment_fallback_kind": result.fallback_kind,
        "content_source": "comment_emoji_fallback" if is_emoji_fallback else "normal",
        "quality_fallback": "comment_emoji_fallback" if is_emoji_fallback else "",
        "fallback_reason": result.fallback_reason,
        "planned_normal_text_emoji": "no" if is_emoji_fallback else "unresolved",
        "ai_generation_result_cache": {},
    })
    mark_attempt_outcome(data, request.attempt_id, "ready", timestamp=_now())
    action.payload = data
    action.candidate_hash = hashlib.sha256(result.content.encode("utf-8")).hexdigest()
    evaluator_evidence = dict(
        (result.quality_audit or {}).get("two_stage_evaluator_evidence") or {},
    )
    action.result = {
        **(action.result or {}),
        "generation_stage": "generation_ready",
        "generation_outcome": "ready",
        "ai_generation_attempt_id": request.attempt_id,
        "comment_quality_audit": result.quality_audit or {},
        "evaluator_evidence": evaluator_evidence,
    }


def persist_generation_failure(
    session: Session,
    request: CommentGenerationRequest,
    detail: str,
    *,
    code: str = "generation_failed",
    evaluator_evidence: dict | None = None,
    tokens: int = 0,
) -> None:
    action = load_attempt_action(session, request)
    evidence = dict(evaluator_evidence or {})
    action.candidate_hash = str(evidence.get("candidate_hash") or "")
    action.result = {**(action.result or {}), "evaluator_evidence": evidence}
    data = dict(action.payload or {})
    data["ai_generation_tokens"] = max(0, int(tokens or 0))
    action.payload = data
    finish_comment_generation_job(
        session, action, request.payload, state="failed", owner=request.claim_owner,
    )
    _fail_before_generation(action, code, detail or "AI 评论生成失败")
    _cas_write_action(session, request, action)
    session.commit()
    _release_runtime_resources(action)


def persist_generation_unknown(
    session: Session,
    request: CommentGenerationRequest,
    generated: GeneratedCommentResult,
    *,
    detail: str,
) -> None:
    action = load_attempt_action(session, request)
    finish_comment_generation_job(
        session, action, request.payload, state="unknown", owner=request.claim_owner,
    )
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
    mark_attempt_outcome(
        data, request.attempt_id, "ai_result_persist_unknown", timestamp=_now(),
    )
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


def load_attempt_action(
    session: Session,
    request: CommentGenerationRequest,
) -> Action:
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


def _cas_write_action(
    session: Session,
    request: CommentGenerationRequest,
    action: Action,
) -> None:
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
    "fail_generation_context",
    "load_attempt_action",
    "mark_provider_call_started",
    "persist_comment_generation_result",
    "persist_generation_failure",
    "persist_generation_unknown",
]
