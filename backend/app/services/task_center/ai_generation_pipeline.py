from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from .ai_generation_dependencies import GenerationDependencies
from .ai_generator import AiGenerationUnavailable, _copy_generated_content_metadata
from .ai_generation_state import validate_output_sequences


@dataclass(frozen=True)
class SlotGenerationResult:
    content: str
    rejection_code: str = ""
    rejection_detail: str = ""
    voice_profile_anchor_rewritten: bool = False


def generate_quality_results(
    session: Session,
    request,
    dependencies: GenerationDependencies,
) -> tuple[list[SlotGenerationResult], int]:
    if request.cached_contents:
        return _filter_stage_contents(request, request.cached_contents), request.cached_tokens
    pending = list(range(len(request.batch_ids)))
    accepted: dict[int, SlotGenerationResult] = {}
    last_rejections: dict[int, SlotGenerationResult] = {}
    total_tokens = 0
    last_error: AiGenerationUnavailable | None = None
    for stage in _fallback_stages(request.config):
        if not pending:
            break
        try:
            contents, tokens = _generate_stage(
                session,
                request,
                pending,
                stage=stage,
                dependencies=dependencies,
            )
        except AiGenerationUnavailable as exc:
            last_error = exc
            continue
        total_tokens += tokens
        results = _filter_stage_contents(request, contents, indexes=pending)
        next_pending: list[int] = []
        for item_index, result in zip(pending, results, strict=True):
            if result.rejection_code:
                last_rejections[item_index] = result
                next_pending.append(item_index)
                continue
            accepted[item_index] = result
        pending = next_pending
    if pending and last_error and not last_rejections:
        raise last_error
    return _ordered_results(request, accepted, last_rejections), total_tokens


def _generate_stage(
    session: Session,
    request,
    indexes: list[int],
    *,
    stage: str,
    dependencies: GenerationDependencies,
) -> tuple[list[str], int]:
    if session.in_transaction():
        raise RuntimeError("Phase B provider call started with an open database transaction")
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
    return contents, tokens


def _stage_config(config: dict, indexes: list[int], stage: str) -> dict:
    slots = []
    source_slots = list(config.get("generation_slots") or [])
    for sequence, index in enumerate(indexes, 1):
        slot = dict(source_slots[index])
        slot["sequence_index"] = sequence
        slot["reply_to_sequence_index"] = sequence if slot.get("reply_to_message_id") else None
        slots.append(slot)
    result = {**config, "generation_slots": slots}
    if stage.startswith("direct_"):
        result.pop("_ai_fallback_stage", None)
    else:
        result["_ai_fallback_stage"] = stage
    return result


def _fallback_stages(config: dict) -> tuple[str, ...]:
    if bool(config.get("require_mimo_draft")):
        return ("direct_mimo",)
    if str(config.get("ai_model") or "").strip():
        return ("direct_configured_model",)
    stages = ["primary_m3"]
    if bool(config.get("_ai_group_model_fallback_enabled", True)):
        stages.append("fallback_m25")
    if bool(config.get("_ai_group_grok_fallback_enabled", True)):
        stages.append("fallback_grok")
    return tuple(stages)


def _filter_stage_contents(
    request,
    contents: list[str],
    *,
    indexes: list[int] | None = None,
) -> list[SlotGenerationResult]:
    selected = indexes or list(range(len(contents)))
    accepted_baseline = list(request.duplicate_baseline_messages)
    results = []
    for item_index, content in zip(selected, contents, strict=True):
        result = _filter_slot(request, item_index, content, baseline=accepted_baseline)
        results.append(result)
        if not result.rejection_code:
            accepted_baseline.append(result.content)
    return results


def _filter_slot(request, index: int, content: str, *, baseline: list[str]) -> SlotGenerationResult:
    from .executors import group_ai_chat

    snapshot = request.quality_snapshots[index]
    quality_item = {"slot": request.config["generation_slots"][index]}
    repaired = group_ai_chat._voice_profile_anchor_repaired_content(
        str(content),
        {"summary": snapshot["account_profile"]},
        quality_item,
    )
    anchor_rewritten = repaired != str(content)
    decision = group_ai_chat._voice_profile_match_decision_for_item(
        repaired,
        {"summary": snapshot["account_profile"]},
        quality_item,
    )
    if int(decision["score"]) <= group_ai_chat.VOICE_PROFILE_MISMATCH_SCORE:
        return SlotGenerationResult(
            repaired,
            "voice_profile_mismatch",
            str(decision["reason"]),
            anchor_rewritten,
        )
    if reason := group_ai_chat._stance_conflict_reason(repaired, snapshot["stance_summary"]):
        return SlotGenerationResult(repaired, "stance_conflict", reason, anchor_rewritten)
    quality, stats = group_ai_chat._quality_filter_ai_messages(
        [repaired],
        baseline,
        chat_mode=request.chat_mode,
        anchor_message_ids=request.context_message_ids,
        fact_anchor_required=request.fact_anchor_required,
        low_confidence_silence_enabled=request.low_confidence_silence_enabled,
        limit=1,
    )
    if not quality:
        code = str(stats.get("skip_reason") or "quality_rejected")
        return SlotGenerationResult(repaired, code, code, anchor_rewritten)
    return SlotGenerationResult(
        _copy_generated_content_metadata(repaired, content),
        voice_profile_anchor_rewritten=anchor_rewritten,
    )


def _ordered_results(request, accepted: dict, rejected: dict) -> list[SlotGenerationResult]:
    missing = SlotGenerationResult("", "quality_rejected", "all_model_stages_rejected")
    return [accepted.get(index) or rejected.get(index) or missing for index in range(len(request.batch_ids))]


__all__ = ["SlotGenerationResult", "generate_quality_results"]
