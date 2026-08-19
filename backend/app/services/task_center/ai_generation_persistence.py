from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from app.models import GenerationJob, Task
from app.services._common import _now

from .ai_generation_commit import commit_generation_action, load_generation_batch
from .ai_generation_pipeline import SlotGenerationResult
from .ai_generation_quality import fail_generation_action, store_generation_quality
from .ai_message_memory import DuplicateMemoryBatch
from .ai_generation_state import (
    GenerationMappingError,
    apply_generated_content_metadata,
    mark_attempt_outcome,
    validate_generation_mapping,
)
from .legacy_anchor_rewrite import VOICE_PROFILE_CONTRACT_VERSION
from .generation_wait import GenerationWaitSpec, defer_generation_wait
from .two_stage_generation import QUALITY_WAIT
from .direct_check_in import (
    DUE_CATCH_UP_CHECK_IN_REASON,
    DUE_CATCH_UP_CHECK_IN_SOURCE,
)


def persist_generation_results(
    session: Session,
    request,
    results: list[SlotGenerationResult],
    *,
    tokens: int,
) -> None:
    batch = load_generation_batch(session, request)
    if len(results) != len(batch):
        raise GenerationMappingError(
            "ai_generation_output_count_mismatch",
            expected_slot_count=len(batch),
            received_slot_count=len(results),
        )
    validate_generation_mapping(
        batch,
        [result.content for result in results],
        generation_slots=list(request.config.get("generation_slots") or []),
    )
    duplicate_batch = DuplicateMemoryBatch(now=_now())
    with session.no_autoflush:
        for index, ((action, payload), result) in enumerate(zip(batch, results, strict=True)):
            _persist_generation_result(
                session,
                request,
                action=action,
                payload=payload,
                result=result,
                tokens=int(tokens or 0) if index == 0 else 0,
                duplicate_batch=duplicate_batch,
            )


def _persist_generation_result(
    session: Session,
    request,
    *,
    action,
    payload,
    result: SlotGenerationResult,
    tokens: int,
    duplicate_batch: DuplicateMemoryBatch,
) -> None:
    if result.rejection_code:
        _persist_generation_rejection(session, request, action=action, result=result)
        return
    data = _generated_payload(payload, result, tokens=tokens)
    mark_attempt_outcome(data, request.attempt_id, "ready", timestamp=_now())
    if not store_generation_quality(
        session, action, payload, data=data, duplicate_batch=duplicate_batch,
    ):
        commit_generation_action(session, request, action)
        return
    action.payload = data
    action.candidate_hash = hashlib.sha256(data["message_text"].encode("utf-8")).hexdigest()
    action.result = {
        **(action.result or {}),
        "generation_stage": "generation_ready",
        "generation_outcome": "ready",
        "ai_generation_attempt_id": request.attempt_id,
        "voice_profile_anchor_rewritten": result.voice_profile_anchor_rewritten,
        "voice_profile_contract_version": VOICE_PROFILE_CONTRACT_VERSION,
        "evaluator_evidence": dict(result.evaluator_evidence),
    }
    commit_generation_action(session, request, action)


def _persist_generation_rejection(
    session: Session,
    request,
    *,
    action,
    result: SlotGenerationResult,
) -> None:
    if result.rejection_code == QUALITY_WAIT:
        _persist_quality_wait(session, request, action=action, result=result)
        return
    fail_generation_action(
        action,
        result.rejection_code,
        result.rejection_detail,
        stage="ai_generation_quality",
    )
    evidence = dict(result.evaluator_evidence)
    action.candidate_hash = str(evidence.get("candidate_hash") or "")
    action.result = {**(action.result or {}), "evaluator_evidence": evidence}
    commit_generation_action(session, request, action)


def _persist_quality_wait(session: Session, request, *, action, result) -> None:
    task = session.get(Task, action.task_id)
    job_id = str(dict(action.payload or {}).get("generation_job_id") or "")
    job = session.get(GenerationJob, job_id) if job_id else None
    if task is None or job is None:
        raise RuntimeError("quality_wait_generation_contract_missing")
    evidence = dict(result.evaluator_evidence)
    action.candidate_hash = str(evidence.get("candidate_hash") or "")
    defer_generation_wait(
        session,
        task,
        action,
        job,
        GenerationWaitSpec(
            stage=QUALITY_WAIT,
            error_code=QUALITY_WAIT,
            error_detail=result.rejection_detail,
            shortfall_kind="quality",
            evaluator_evidence=evidence,
        ),
    )
    commit_generation_action(session, request, action)


def _generated_payload(payload, result: SlotGenerationResult, *, tokens: int) -> dict:
    data = apply_generated_content_metadata(payload.model_dump(mode="json"), result.content)
    data.update({
        "message_text": str(result.content).strip(),
        "ai_generation_status": "ready",
        "ai_generation_tokens": tokens,
        "ai_generation_result_cache": {},
        "voice_profile_contract_version": VOICE_PROFILE_CONTRACT_VERSION,
    })
    quality_fallback = result.quality_fallback or str(
        getattr(result.content, "quality_fallback", "") or "",
    )
    if quality_fallback:
        _apply_quality_fallback(data, result, quality_fallback=quality_fallback)
    return data


def _apply_quality_fallback(
    data: dict,
    result: SlotGenerationResult,
    *,
    quality_fallback: str,
) -> None:
    is_check_in = quality_fallback == "check_in_fallback" or str(result.content).strip() == "签到"
    data.update({
        "act_type": "check_in" if is_check_in else quality_fallback,
        "human_quality_decision": (
            "check_in_fallback" if is_check_in else "explicit_static_quality_fallback"
        ),
        "quality_fallback": "check_in_fallback" if is_check_in else quality_fallback,
        "content_source": _fallback_content_source(data, result, is_check_in=is_check_in),
        "generation_source": (
            "static_safe_fallback" if is_check_in else data.get("generation_source", "")
        ),
        "fallback_reason": result.fallback_reason or data.get("fallback_reason", ""),
    })


def _fallback_content_source(
    data: dict,
    result: SlotGenerationResult,
    *,
    is_check_in: bool,
) -> str:
    if not is_check_in:
        return str(data.get("content_source") or "")
    if result.fallback_reason == DUE_CATCH_UP_CHECK_IN_REASON:
        return DUE_CATCH_UP_CHECK_IN_SOURCE
    return "check_in_fallback"


__all__ = ["persist_generation_results"]
