from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from sqlalchemy.orm import Session

from app.services._common import _now
from app.timezone import as_beijing

from .ai_generation_dependencies import GenerationDependencies
from .ai_generator import (
    AI_CONTENT_REQUEST_TIMEOUT_SECONDS,
    AiGenerationUnavailable,
    GeneratedContent,
    ProviderRouteDeferred,
)
from .ai_generation_state import validate_output_sequences, validate_output_slot_ids
from .ai_generation_support import (
    AI_GENERATION_DEADLINE_BUDGET_EXHAUSTED,
    SlotGenerationResult,
    TwoStageRuntime as _TwoStageRuntime,
)
from .ai_generation_stage_config import (
    fallback_stages as _fallback_stages,
    stage_config as _stage_config,
    two_stage_plan_slots as _two_stage_plan_slots,
)
from .message_brief import BATCH_FINGERPRINT_LIMIT, structural_fingerprint
from .two_stage_generation import (
    QUALITY_WAIT,
    TWO_STAGE_REALIZE_ATTEMPTS,
    TwoStageRealizeError,
    plan_message_briefs,
    realize_message_content,
    two_stage_enabled,
)
from .ai_generation_quality_filters import (
    _filter_slot, _filter_stage_contents, _candidate_frequency_payload, _ordered_results,
)


def generate_quality_results(
    session: Session,
    request,
    dependencies: GenerationDependencies,
) -> tuple[list[SlotGenerationResult], int]:
    if request.cached_contents:
        return _cached_quality_results(session, request), request.cached_tokens
    if catch_up := _due_catch_up_results(request):
        return catch_up, 0
    if two_stage_enabled(getattr(request, "config", {})):
        return _generate_two_stage_results(session, request, dependencies)
    return _generate_legacy_quality_results(session, request, dependencies)


def _generate_legacy_quality_results(session: Session, request, dependencies: GenerationDependencies):
    pending = list(range(len(request.batch_ids)))
    accepted, last_rejections = {}, {}
    total_tokens = 0
    last_error: AiGenerationUnavailable | None = None
    for stage in _fallback_stages(request.config):
        if not pending:
            break
        _require_provider_attempt_budget(request)
        try:
            contents, tokens = _generate_stage(
                session,
                request,
                pending,
                stage=stage,
                dependencies=dependencies,
            )
        except ProviderRouteDeferred:
            _close_failed_stage_transaction(session)
            raise
        except AiGenerationUnavailable as exc:
            _close_failed_stage_transaction(session)
            last_error = exc
            continue
        total_tokens += tokens
        stage_accepted, stage_rejected, pending = _partition_stage_results(
            request,
            contents,
            indexes=pending,
        )
        accepted.update(stage_accepted)
        last_rejections.update(stage_rejected)
    _apply_static_quantity_fallback(
        session,
        request,
        pending=pending,
        accepted=accepted,
        rejected=last_rejections,
    )
    remaining = [index for index in pending if index not in accepted]
    if remaining and last_error and not last_rejections:
        raise last_error
    return _ordered_results(request, accepted, last_rejections), total_tokens


def _close_failed_stage_transaction(session: Session) -> None:
    if session.in_transaction():
        session.rollback()


def _partition_stage_results(
    request,
    contents: list[str],
    *,
    indexes: list[int],
) -> tuple[dict[int, SlotGenerationResult], dict[int, SlotGenerationResult], list[int]]:
    results = _filter_stage_contents(request, contents, indexes=indexes)
    accepted: dict[int, SlotGenerationResult] = {}
    rejected: dict[int, SlotGenerationResult] = {}
    pending: list[int] = []
    for item_index, result in zip(indexes, results, strict=True):
        if result.rejection_code:
            rejected[item_index] = result
            pending.append(item_index)
            continue
        accepted[item_index] = result
    return accepted, rejected, pending


def _cached_quality_results(session: Session, request) -> list[SlotGenerationResult]:
    cached_fallbacks = _cached_static_fallbacks(request.cached_contents)
    accepted = cached_fallbacks if _static_fallback_enabled(request) else {}
    rejected = {
        index: SlotGenerationResult(
            result.content, "static_fallback_disabled", "static_fallback_disabled"
        )
        for index, result in cached_fallbacks.items()
        if index not in accepted
    }
    plain_indexes = [
        index
        for index in range(len(request.cached_contents))
        if index not in cached_fallbacks
    ]
    plain_contents = [request.cached_contents[index] for index in plain_indexes]
    results = _filter_stage_contents(request, plain_contents, indexes=plain_indexes)
    accepted.update(
        {
            index: result
            for index, result in zip(plain_indexes, results)
            if not result.rejection_code
        }
    )
    rejected.update(
        {
            index: result
            for index, result in zip(plain_indexes, results)
            if result.rejection_code
        }
    )
    _apply_static_quantity_fallback(
        session,
        request,
        pending=list(rejected),
        accepted=accepted,
        rejected=rejected,
    )
    return _ordered_results(request, accepted, rejected)


def _cached_static_fallbacks(contents: list[str]) -> dict[int, SlotGenerationResult]:
    return {
        index: SlotGenerationResult(
            content,
            quality_fallback="check_in_fallback",
            fallback_reason=str(
                getattr(content, "fallback_reason", "") or "cached_static_fallback"
            ),
        )
        for index, content in enumerate(contents)
        if getattr(content, "quality_fallback", "") == "check_in_fallback"
        or str(content).strip() == "签到"
    }


def _due_catch_up_results(request) -> list[SlotGenerationResult]:
    config = getattr(request, "config", {}) or {}
    if not config.get("_ai_group_due_catch_up_required"):
        return []
    if not _static_fallback_enabled(request):
        return []
    slots = list(config.get("generation_slots") or [])
    if len(slots) != len(request.batch_ids):
        return []
    if not slots or any(not _has_fallback_quantity_slot(slot) for slot in slots):
        return []
    reason = "due_catch_up_provider_budget_exhausted"
    return [
        SlotGenerationResult(
            _check_in_fallback_content(slot, index, reason),
            quality_fallback="check_in_fallback",
            fallback_reason=reason,
        )
        for index, slot in enumerate(slots)
    ]


def _apply_static_quantity_fallback(
    session: Session,
    request,
    *,
    pending: list[int],
    accepted: dict[int, SlotGenerationResult],
    rejected: dict[int, SlotGenerationResult],
) -> None:
    if not _static_fallback_enabled(request):
        return
    slots = list(request.config.get("generation_slots") or [])
    del session
    for index in pending:
        slot = slots[index]
        if not _has_fallback_quantity_slot(slot):
            continue
        reason = (
            rejected.get(index) or SlotGenerationResult("")
        ).rejection_code or "all_model_stages_rejected"
        if reason == "negative_lexicon_match":
            continue
        content = _check_in_fallback_content(slot, index, reason)
        accepted[index] = SlotGenerationResult(
            content,
            quality_fallback="check_in_fallback",
            fallback_reason=reason,
        )
        rejected.pop(index, None)


def _static_fallback_enabled(request) -> bool:
    config = getattr(request, "config", {}) or {}
    # Two-stage 合同（PRD §5.4）：质量耗尽进入 quality_wait，禁止签到兜底补量。
    if two_stage_enabled(config):
        return False
    # Explicit single-model requests keep quality rejections visible; they do not
    # enter the multi-stage default static fallback chain.
    if str(config.get("ai_model") or "").strip():
        return False
    if not bool(config.get("_ai_group_static_fallback_enabled", True)):
        return False
    slots = list(config.get("generation_slots") or [])
    return any(_has_fallback_quantity_slot(slot) for slot in slots)


def _generate_two_stage_results(
    session: Session,
    request,
    dependencies: GenerationDependencies,
) -> tuple[list[SlotGenerationResult], int]:
    history_lines = str(request.history or "").splitlines()
    plans, brief_tokens = plan_message_briefs(
        session,
        request.tenant_id,
        request.config,
        history_lines=history_lines,
        slots=_two_stage_plan_slots(request),
        planner=dependencies.brief_planner,
    )
    accepted: dict[int, SlotGenerationResult] = {}
    rejected: dict[int, SlotGenerationResult] = {}
    runtime = _TwoStageRuntime(
        session,
        request,
        dependencies,
        history_lines,
        list(request.duplicate_baseline_messages),
        list(getattr(request, "vocabulary_frequency_baseline", [])),
        {},
    )
    total_tokens = brief_tokens
    for index, plan in enumerate(plans):
        result, spent = _realize_two_stage_plan(runtime, plan, index)
        total_tokens += spent
        (rejected if result.rejection_code else accepted)[index] = result
    return _ordered_results(request, accepted, rejected), total_tokens


def _realize_two_stage_plan(
    runtime: _TwoStageRuntime,
    plan,
    index: int,
) -> tuple[SlotGenerationResult, int]:
    if plan.rejection_code:
        return _two_stage_rejected(
            plan.rejection_code, plan.rejection_detail, plan.slot_id, index
        ), 0
    if plan.brief is None or plan.brief.speech_act == "silence":
        detail = "brief_silence：上下文不支持安全发言，宁可沉默不造句"
        return _two_stage_rejected(QUALITY_WAIT, detail, plan.slot_id, index), 0
    feedback = ""
    spent_tokens = 0
    for attempt_index in range(1, TWO_STAGE_REALIZE_ATTEMPTS + 1):
        result, spent = _realize_two_stage_attempt(
            runtime,
            plan,
            index,
            feedback=feedback,
            attempt_index=attempt_index,
        )
        spent_tokens += spent
        if not result.rejection_code:
            runtime.baseline.append(str(result.content))
            runtime.vocabulary_frequency_baseline.insert(
                0,
                _candidate_frequency_payload(
                    runtime.request, index, str(result.content)
                ),
            )
            return result, spent_tokens
        feedback = f"{result.rejection_code}:{result.rejection_detail}"
    detail = f"realize 预算耗尽，最后拒绝码={result.rejection_code or 'unknown'}"
    rejected = _two_stage_rejected(
        QUALITY_WAIT,
        detail,
        plan.slot_id,
        index,
        evaluator_evidence=result.evaluator_evidence,
    )
    return rejected, spent_tokens


def _realize_two_stage_attempt(
    runtime: _TwoStageRuntime,
    plan,
    index: int,
    *,
    feedback: str,
    attempt_index: int,
) -> tuple[SlotGenerationResult, int]:
    _require_provider_attempt_budget(runtime.request)
    try:
        content, meta, spent = realize_message_content(
            runtime.session,
            runtime.request.tenant_id,
            runtime.request.config,
            plan,
            history_lines=runtime.history_lines,
            rejection_feedback=feedback,
            realization_attempt=attempt_index,
            realizer=runtime.dependencies.brief_realizer,
            reviewer=runtime.dependencies.semantic_reviewer,
        )
    except TwoStageRealizeError as exc:
        return SlotGenerationResult(
            "",
            exc.code,
            exc.code,
            evaluator_evidence=exc.evidence,
        ), exc.tokens
    mapped = GeneratedContent(content, slot_id=plan.slot_id, sequence_index=index + 1)
    gate = _filter_slot(
        runtime.request,
        index,
        mapped,
        baseline=runtime.baseline,
        frequency_baseline=runtime.vocabulary_frequency_baseline,
    )
    gate = replace(gate, evaluator_evidence=dict(meta))
    return _two_stage_structural_gate(runtime, plan, gate), spent


def _two_stage_structural_gate(
    runtime: _TwoStageRuntime,
    plan,
    gate: SlotGenerationResult,
) -> SlotGenerationResult:
    if gate.rejection_code:
        return gate
    fingerprint = structural_fingerprint(plan.brief, str(gate.content))
    count = runtime.fingerprint_counts.get(fingerprint, 0)
    if count >= BATCH_FINGERPRINT_LIMIT:
        return replace(
            gate,
            rejection_code="structural_duplicate",
            rejection_detail=f"同批结构指纹已出现 {BATCH_FINGERPRINT_LIMIT} 次",
        )
    runtime.fingerprint_counts[fingerprint] = count + 1
    return gate


def _two_stage_rejected(
    code: str,
    detail: str,
    slot_id: str,
    index: int,
    *,
    evaluator_evidence: dict | None = None,
) -> SlotGenerationResult:
    """被拒 slot 的映射守恒占位：内容仅用于数量映射校验与审计，不进入发送。

    persist_generation_results 对 rejection_code 非空的结果走 fail 分支，
    message_text 永不写入该占位。
    """
    marker = GeneratedContent(
        f"[{code}:{index + 1}]",
        generation_source="two_stage_quality_wait",
        slot_id=slot_id,
        sequence_index=index + 1,
    )
    return SlotGenerationResult(
        marker,
        code,
        detail,
        evaluator_evidence=dict(evaluator_evidence or {}),
    )


def _has_fallback_quantity_slot(slot: dict) -> bool:
    return bool(
        str(slot.get("primary_quantity_slot_id") or "").strip()
        and slot.get("content_obligation_fallback_ready") is True
        and not slot.get("reply_to_message_id")
        and not str(slot.get("material_intent") or "").strip()
    )


def _require_provider_attempt_budget(request) -> None:
    from .generation_invocation_budget import TIMING_CONFIG_KEY, provider_invocation_timeout

    config = getattr(request, "config", {}) or {}
    if TIMING_CONFIG_KEY in config or config.get("engagement_contract_version") == "unified_engagement_v1":
        provider_invocation_timeout(config, legacy_timeout=AI_CONTENT_REQUEST_TIMEOUT_SECONDS, now_value=_now())
        return
    raw_deadline = str(
        (getattr(request, "config", {}) or {}).get(
            "_ai_generation_latest_safe_send_at",
        )
        or ""
    ).strip()
    if not raw_deadline:
        return
    try:
        deadline = datetime.fromisoformat(raw_deadline)
    except ValueError as exc:
        raise AiGenerationUnavailable("ai_generation_deadline_invalid") from exc
    remaining = (as_beijing(deadline) - as_beijing(_now())).total_seconds()
    if remaining < AI_CONTENT_REQUEST_TIMEOUT_SECONDS:
        raise AiGenerationUnavailable(AI_GENERATION_DEADLINE_BUDGET_EXHAUSTED)


def _check_in_fallback_content(slot: dict, index: int, reason: str) -> GeneratedContent:
    from app.services.task_center.conversation_content_quality import CHECK_IN_TEXT

    return GeneratedContent(
        CHECK_IN_TEXT,
        generation_source="static_safe_fallback",
        quality_fallback="check_in_fallback",
        fallback_stage="static_safe_fallback",
        fallback_reason=reason,
        slot_id=str(slot.get("slot_id") or ""),
        sequence_index=index + 1,
        reply_to_sequence_index=(
            index + 1 if slot.get("reply_to_message_id") else None
        ),
    )


def _generate_stage(
    session: Session,
    request,
    indexes: list[int],
    *,
    stage: str,
    dependencies: GenerationDependencies,
) -> tuple[list[str], int]:
    if session.in_transaction():
        raise RuntimeError(
            "Phase B provider call started with an open database transaction"
        )
    config = _stage_config(request.config, indexes, stage)
    if request.is_reply:
        contents, tokens = dependencies.reply_generator(
            session,
            request.tenant_id,
            config,
            reply_targets=[request.reply_targets[index] for index in indexes],
            target_label=request.target_label,
            history=request.history,
        )
    else:
        contents, tokens = dependencies.normal_generator(
            session,
            request.tenant_id,
            config,
            count=len(indexes),
            target_label=request.target_label,
            history=request.history,
        )
    validate_output_sequences(contents, len(indexes), is_reply=request.is_reply)
    validate_output_slot_ids(contents, config["generation_slots"])
    return contents, tokens


__all__ = ["SlotGenerationResult", "generate_quality_results"]
