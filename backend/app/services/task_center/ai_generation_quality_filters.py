from __future__ import annotations

from .ai_adult_content_contract import validate_adult_content_contract
from .ai_generator import _copy_generated_content_metadata
from .ai_generation_support import SlotGenerationResult, legacy_negative_match
from .ai_group_vocabulary_frequency import vocabulary_frequency_violation_from_rows
from .ai_group_vocabulary_sampling import (
    extract_vocabulary_usage, protected_frequency_phrases, surface_phrase_fingerprints,
)


def _filter_stage_contents(
    request,
    contents: list[str],
    *,
    indexes: list[int] | None = None,
) -> list[SlotGenerationResult]:
    selected = indexes or list(range(len(contents)))
    accepted_baseline = list(request.duplicate_baseline_messages)
    frequency_baseline = list(getattr(request, "vocabulary_frequency_baseline", []))
    results = []
    for item_index, content in zip(selected, contents, strict=True):
        result = _filter_slot(
            request,
            item_index,
            content,
            baseline=accepted_baseline,
            frequency_baseline=frequency_baseline,
        )
        results.append(result)
        if not result.rejection_code:
            accepted_baseline.append(result.content)
            frequency_baseline.insert(
                0,
                _candidate_frequency_payload(request, item_index, str(result.content)),
            )
    return results


def _filter_slot(
    request,
    index: int,
    content: str,
    *,
    baseline: list[str],
    frequency_baseline: list[dict] | None = None,
) -> SlotGenerationResult:
    from .ai_group_prompt import sanitize_group_message_text

    cleaned_text = sanitize_group_message_text(str(content or ""))
    if not cleaned_text or len(cleaned_text.strip()) < 2:
        return SlotGenerationResult("", "quality_rejected", "link_restricted_or_empty")
    quality_item = {"slot": request.config["generation_slots"][index]}
    mapped = _copy_generated_content_metadata(str(cleaned_text), content)
    mapped.sequence_index = index + 1
    if violation := validate_adult_content_contract(
        request.config, content=str(cleaned_text), history=request.history,
        slot=quality_item["slot"],
    ):
        return SlotGenerationResult(mapped, violation.code, violation.detail)
    if legacy_negative_match(request, str(cleaned_text)):
        return SlotGenerationResult(
            mapped, "negative_lexicon_match", "negative_lexicon_match"
        )
    if request.config["generation_slots"][index].get("reply_to_message_id"):
        mapped.reply_to_sequence_index = index + 1
    if violation := _profile_violation(request, index, content, mapped=mapped):
        return violation
    return _history_quality_result(request, index, content, mapped=mapped,
        baseline=baseline, frequency_baseline=frequency_baseline)


def _profile_violation(request, index, content, *, mapped):
    from .executors import group_ai_chat

    snapshot = request.quality_snapshots[index]
    quality_item = {"slot": request.config["generation_slots"][index]}
    decision = group_ai_chat._voice_profile_match_decision_for_item(
        str(content),
        {"summary": snapshot["account_profile"]},
        quality_item,
    )
    if int(decision["score"]) <= group_ai_chat.VOICE_PROFILE_MISMATCH_SCORE:
        return SlotGenerationResult(
            mapped,
            "voice_profile_mismatch",
            str(decision["reason"]),
            False,
        )
    if reason := group_ai_chat._stance_conflict_reason(
        str(content), snapshot["stance_summary"]
    ):
        return SlotGenerationResult(mapped, "stance_conflict", reason)
    return None


def _history_quality_result(request, index, content, *, mapped, baseline, frequency_baseline):
    from .executors import group_ai_chat

    cleaned_text = str(mapped)
    frequency_payload = _candidate_frequency_payload(request, index, str(cleaned_text))
    frequency_violation = vocabulary_frequency_violation_from_rows(
        list(frequency_baseline or []), data=frequency_payload
    )
    if frequency_violation:
        return SlotGenerationResult(
            mapped,
            "vocabulary_frequency_exceeded",
            frequency_violation,
        )
    quality, stats = group_ai_chat._quality_filter_ai_messages(
        [str(content)],
        baseline,
        chat_mode=request.chat_mode,
        anchor_message_ids=request.context_message_ids,
        fact_anchor_required=request.fact_anchor_required,
        low_confidence_silence_enabled=request.low_confidence_silence_enabled,
        limit=1,
    )
    if not quality:
        code = str(stats.get("skip_reason") or "quality_rejected")
        return SlotGenerationResult(mapped, code, code)
    return SlotGenerationResult(mapped)


def _candidate_frequency_payload(request, index: int, content: str) -> dict:
    slot = request.config["generation_slots"][index]
    route_family = str(slot.get("route_family") or "general")
    used_ids, used_terms = extract_vocabulary_usage(content, route_family=route_family)
    excluded = protected_frequency_phrases(
        dict(slot.get("topic_direction") or {}),
        dict(slot.get("teacher_target") or {}),
    )
    return {
        "vocabulary_used_ids": list(used_ids),
        "vocabulary_used_term_ids": list(used_terms),
        "surface_phrase_fingerprints": list(
            surface_phrase_fingerprints(content, excluded_phrases=excluded)
        ),
    }


def _ordered_results(
    request, accepted: dict, rejected: dict
) -> list[SlotGenerationResult]:
    missing = SlotGenerationResult("", "quality_rejected", "all_model_stages_rejected")
    return [
        accepted.get(index) or rejected.get(index) or missing
        for index in range(len(request.batch_ids))
    ]
