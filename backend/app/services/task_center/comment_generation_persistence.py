from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from sqlalchemy.orm import Session

from app.models import Action, GenerationJob, Task
from app.services._common import _now

from .ai_generation_state import mark_attempt_outcome
from .ai_generator import AiGenerationUnavailable
from .comment_generation_job import (
    finish_comment_generation_job,
)
from .comment_generation_action_store import (
    cas_write_action as _cas_write_action,
    load_attempt_action,
)
from .comment_generation_pipeline import (
    REPLY_QUALITY_SHORTFALL,
    CommentGenerationRequest,
    GeneratedCommentResult,
)
from .comment_generation_result import (
    evaluate_legacy_generated_comment,
    generated_comment_decision,
)
from .runtime_resources import _release_runtime_resources
from .generation_wait import GenerationWaitSpec, defer_generation_wait
from .two_stage_generation import QUALITY_WAIT


COMMENT_QUALITY_CONTRACT_VERSION = "channel_comment_grounding_quality_v1"
COMMENT_QUALITY_WAIT_CODES = frozenset({QUALITY_WAIT, REPLY_QUALITY_SHORTFALL})


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


def defer_generation_provider(
    session: Session,
    request: CommentGenerationRequest,
    *,
    retry_after_seconds: int,
) -> None:
    action = load_attempt_action(session, request)
    next_retry_at = _now() + timedelta(seconds=max(1, retry_after_seconds))
    task = session.get(Task, action.task_id)
    job = session.get(GenerationJob, request.payload.generation_job_id)
    if task is None or job is None:
        raise RuntimeError("provider_wait_generation_contract_missing")
    defer_generation_wait(
        session,
        task,
        action,
        job,
        GenerationWaitSpec(
            stage="waiting_provider",
            error_code="provider_route_deferred",
            error_detail="provider route temporarily unavailable",
            shortfall_kind="provider_capacity",
            evaluator_evidence={},
            next_retry_at=next_retry_at,
        ),
    )
    data = dict(action.payload or {})
    mark_attempt_outcome(
        data,
        request.attempt_id,
        str(action.result.get("generation_outcome") or "waiting_provider"),
        timestamp=_now(),
    )
    action.payload = data
    _cas_write_action(session, request, action)
    session.commit()
    _release_runtime_resources(action)


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
        if decision.code in COMMENT_QUALITY_WAIT_CODES:
            _defer_comment_quality_wait(
                session,
                request,
                action,
                code=decision.code,
                detail=decision.detail,
                evidence=decision.audit or {},
            )
            _cas_write_action(session, request, action)
            return
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
    is_emoji_fallback = result.fallback_kind in {"emoji_text", "unicode_emoji"}
    is_image_fallback = result.fallback_kind == "image_meme"
    content_source = _ready_content_source(result)
    candidate_hash = hashlib.sha256(result.content.encode("utf-8")).hexdigest()
    accepted_fields = _accepted_content_fields(
        result,
        candidate_hash=candidate_hash,
        is_text_fallback=is_emoji_fallback,
    )
    data.update(_ready_payload_fields(
        result,
        content_source=content_source,
        fallback=is_emoji_fallback or is_image_fallback,
        text_fallback=is_emoji_fallback,
    ))
    data.update(accepted_fields)
    mark_attempt_outcome(data, request.attempt_id, "ready", timestamp=_now())
    action.payload = data
    _set_ready_candidate_hash(
        action, result, candidate_hash=candidate_hash, image=is_image_fallback,
    )
    evaluator_evidence = dict(
        (result.quality_audit or {}).get("two_stage_evaluator_evidence") or {},
    )
    action.result = {
        **(action.result or {}),
        "generation_stage": "generation_ready",
        "generation_outcome": "ready",
        "ai_generation_attempt_id": request.attempt_id,
        "comment_quality_audit": result.quality_audit or {},
        "comment_fallback_selection": result.selection_metadata or {},
        "evaluator_evidence": evaluator_evidence,
    }


def _ready_payload_fields(
    result: GeneratedCommentResult,
    *,
    content_source: str,
    fallback: bool,
    text_fallback: bool,
) -> dict:
    return {
        "comment_text": result.content,
        "comment_media_segment": result.media_segment or {},
        "comment_fallback_selection": result.selection_metadata or {},
        "ai_generation_status": "ready",
        "comment_lifecycle_state": "fallback_ready" if fallback else "quality_accepted",
        "ai_generation_tokens": max(0, int(result.tokens or 0)),
        "comment_generation_attempts": list(result.attempts),
        "comment_fallback_kind": result.fallback_kind,
        "content_source": content_source,
        "quality_fallback": content_source if fallback else "",
        "fallback_reason": result.fallback_reason,
        "planned_normal_text_emoji": "no" if text_fallback else "unresolved",
        "ai_generation_result_cache": {},
    }


def _ready_content_source(result: GeneratedCommentResult) -> str:
    if result.fallback_kind == "image_meme":
        return "comment_image_meme_fallback"
    if result.fallback_kind == "unicode_emoji":
        return "comment_unicode_emoji_fallback"
    if result.fallback_kind == "emoji_text":
        return "comment_emoji_fallback"
    return "normal"


def _set_ready_candidate_hash(
    action: Action,
    result: GeneratedCommentResult,
    *,
    candidate_hash: str,
    image: bool,
) -> None:
    if image:
        candidate_identity = json.dumps(
            result.media_segment or {}, sort_keys=True, separators=(",", ":"),
        )
        action.candidate_hash = hashlib.sha256(candidate_identity.encode("utf-8")).hexdigest()
        return
    action.candidate_hash = candidate_hash


def _accepted_content_fields(
    result: GeneratedCommentResult,
    *,
    candidate_hash: str,
    is_text_fallback: bool,
) -> dict[str, str]:
    quality_version = str(
        (result.quality_audit or {}).get("quality_contract_version")
        or COMMENT_QUALITY_CONTRACT_VERSION
    )
    if is_text_fallback:
        return {
            "accepted_content_text": "",
            "accepted_content_hash": "",
            "fallback_content_text": result.content,
            "fallback_content_hash": candidate_hash,
            "quality_contract_version": quality_version,
        }
    if result.fallback_kind == "image_meme":
        return {
            "accepted_content_text": "",
            "accepted_content_hash": "",
            "fallback_content_text": "",
            "fallback_content_hash": "",
            "quality_contract_version": quality_version,
        }
    return {
        "accepted_content_text": result.content,
        "accepted_content_hash": candidate_hash,
        "fallback_content_text": "",
        "fallback_content_hash": "",
        "quality_contract_version": quality_version,
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
    if code in COMMENT_QUALITY_WAIT_CODES:
        _defer_comment_quality_wait(
            session,
            request,
            action,
            code=code,
            detail=detail,
            evidence=evidence,
        )
        _cas_write_action(session, request, action)
        session.commit()
        _release_runtime_resources(action)
        return
    finish_comment_generation_job(
        session, action, request.payload, state="failed", owner=request.claim_owner,
    )
    _fail_before_generation(action, code, detail or "AI 评论生成失败")
    _cas_write_action(session, request, action)
    session.commit()
    _release_runtime_resources(action)


def _defer_comment_quality_wait(
    session: Session,
    request: CommentGenerationRequest,
    action: Action,
    *,
    code: str,
    detail: str,
    evidence: dict,
) -> None:
    task = session.get(Task, action.task_id)
    job = session.get(GenerationJob, request.payload.generation_job_id)
    if task is None or job is None:
        raise RuntimeError("quality_wait_generation_contract_missing")
    action.candidate_hash = str(evidence.get("candidate_hash") or "")
    defer_generation_wait(
        session,
        task,
        action,
        job,
        GenerationWaitSpec(
            stage=code,
            error_code=code,
            error_detail=detail,
            shortfall_kind="quality",
            evaluator_evidence=evidence,
        ),
    )


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
        "comment_lifecycle_state": "generation_result_persist_unknown",
        "ai_generation_result_cache": {
            "content": generated.content,
            "tokens": max(0, int(generated.tokens or 0)),
            "fallback_kind": generated.fallback_kind,
            "fallback_reason": generated.fallback_reason,
            "attempts": list(generated.attempts),
            "media_segment": generated.media_segment or {},
            "selection_metadata": generated.selection_metadata or {},
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


def persist_provider_result_unknown(
    session: Session,
    request: CommentGenerationRequest,
    *,
    detail: str,
) -> None:
    action = load_attempt_action(session, request)
    finish_comment_generation_job(
        session, action, request.payload, state="unknown", owner=request.claim_owner,
    )
    data = dict(action.payload or {})
    data.update({
        "ai_generation_status": "provider_result_unknown",
        "comment_lifecycle_state": "provider_result_unknown",
    })
    mark_attempt_outcome(
        data, request.attempt_id, "provider_result_unknown", timestamp=_now(),
    )
    action.payload = data
    action.result = {
        **(action.result or {}),
        "success": False,
        "error_code": "provider_result_unknown",
        "error_message": detail or "Provider 返回状态未知",
        "validation_stage": "ai_generation_provider",
        "generation_stage": "provider_result_unknown",
        "generation_outcome": "provider_result_unknown",
    }
    _cas_write_action(session, request, action)
    session.commit()


def _fail_before_generation(
    action: Action,
    code: str,
    detail: str,
    *,
    stage: str = "ai_generation",
) -> None:
    data = dict(action.payload or {})
    data["ai_generation_status"] = code
    data["comment_lifecycle_state"] = "pre_gateway_failed"
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


__all__ = [
    "fail_generation_context",
    "defer_generation_provider",
    "load_attempt_action",
    "mark_provider_call_started",
    "persist_comment_generation_result",
    "persist_generation_failure",
    "persist_generation_unknown",
]
