from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy.orm import Session

from .ai_generation_dependencies import GenerationDependencies
from .ai_generator import AiGenerationUnavailable, GeneratedContent, _copy_generated_content_metadata
from .ai_generation_state import validate_output_sequences, validate_output_slot_ids


@dataclass(frozen=True)
class SlotGenerationResult:
    content: str
    rejection_code: str = ""
    rejection_detail: str = ""
    voice_profile_anchor_rewritten: bool = False
    quality_fallback: str = ""
    fallback_reason: str = ""


def generate_quality_results(
    session: Session,
    request,
    dependencies: GenerationDependencies,
) -> tuple[list[SlotGenerationResult], int]:
    if request.cached_contents:
        return _cached_quality_results(request), request.cached_tokens
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
    _apply_static_coverage_fallback(request, pending, accepted, last_rejections)
    remaining = [index for index in pending if index not in accepted]
    if remaining and last_error and not last_rejections:
        raise last_error
    return _ordered_results(request, accepted, last_rejections), total_tokens


def _cached_quality_results(request) -> list[SlotGenerationResult]:
    cached_fallbacks = _cached_static_fallbacks(request.cached_contents)
    accepted = cached_fallbacks if _static_fallback_enabled(request) else {}
    rejected = {
        index: SlotGenerationResult(result.content, "static_fallback_disabled", "static_fallback_disabled")
        for index, result in cached_fallbacks.items()
        if index not in accepted
    }
    plain_indexes = [
        index for index in range(len(request.cached_contents))
        if index not in cached_fallbacks
    ]
    plain_contents = [request.cached_contents[index] for index in plain_indexes]
    results = _filter_stage_contents(request, plain_contents, indexes=plain_indexes)
    accepted.update({index: result for index, result in zip(plain_indexes, results) if not result.rejection_code})
    rejected.update({index: result for index, result in zip(plain_indexes, results) if result.rejection_code})
    _apply_static_coverage_fallback(request, list(rejected), accepted, rejected)
    return _ordered_results(request, accepted, rejected)


def _cached_static_fallbacks(contents: list[str]) -> dict[int, SlotGenerationResult]:
    return {
        index: SlotGenerationResult(
            content,
            quality_fallback="emoji_react",
            fallback_reason=str(getattr(content, "fallback_reason", "") or "cached_static_fallback"),
        )
        for index, content in enumerate(contents)
        if getattr(content, "quality_fallback", "") == "emoji_react"
    }


def _apply_static_coverage_fallback(
    request,
    pending: list[int],
    accepted: dict[int, SlotGenerationResult],
    rejected: dict[int, SlotGenerationResult],
) -> None:
    if not _static_fallback_enabled(request):
        return
    slots = list(request.config.get("generation_slots") or [])
    used_contents = {str(result.content) for result in accepted.values()}
    for index in pending:
        slot = slots[index]
        if not str(slot.get("coverage_ledger_id") or "").strip():
            continue
        reason = (rejected.get(index) or SlotGenerationResult("")).rejection_code or "all_model_stages_rejected"
        content = _unique_static_emoji_content(slot, index, reason, used_contents)
        accepted[index] = SlotGenerationResult(
            content,
            quality_fallback="emoji_react",
            fallback_reason=reason,
        )
        used_contents.add(str(content))
        rejected.pop(index, None)


def _static_fallback_enabled(request) -> bool:
    config = request.config
    return bool(
        not request.is_reply
        and not config.get("require_mimo_draft")
        and not str(config.get("ai_model") or "").strip()
        and config.get("_ai_group_static_fallback_enabled", True)
    )


def _unique_static_emoji_content(
    slot: dict,
    index: int,
    reason: str,
    used_contents: set[str],
) -> GeneratedContent:
    salt = 0
    while True:
        content = _static_emoji_content(slot, index, reason, salt=salt)
        if str(content) not in used_contents:
            return content
        salt += 1


def _static_emoji_content(slot: dict, index: int, reason: str, *, salt: int = 0) -> GeneratedContent:
    return GeneratedContent(
        _static_emoji_text(slot, salt=salt),
        generation_source="static_safe_fallback",
        quality_fallback="emoji_react",
        fallback_stage="static_safe_fallback",
        fallback_reason=reason,
        slot_id=str(slot.get("slot_id") or ""),
        sequence_index=index + 1,
    )


def _static_emoji_text(slot: dict, *, salt: int = 0) -> str:
    from math import comb

    pool = _STATIC_EMOJI_POOL
    capacity = comb(len(pool), 4)
    rank = int.from_bytes(sha256(_static_emoji_seed(slot, salt).encode()).digest()[:8], "big")
    distributed_rank = (
        rank * STATIC_EMOJI_PERMUTATION_FACTOR + STATIC_EMOJI_PERMUTATION_OFFSET
    ) % capacity
    indexes = _unrank_combination(len(pool), 4, distributed_rank)
    return "".join(pool[index] for index in indexes)


def _static_emoji_seed(slot: dict, salt: int) -> str:
    keys = (
        "coverage_window_date",
        "group_id",
        "account_id",
        "coverage_ledger_id",
        "coverage_account_completed_before_action",
        "slot_id",
    )
    return "|".join([*(str(slot.get(key) or "") for key in keys), str(salt)])


def _unrank_combination(size: int, choose: int, rank: int) -> tuple[int, ...]:
    from math import comb

    indexes: list[int] = []
    candidate = 0
    for remaining in range(choose, 0, -1):
        while rank >= comb(size - candidate - 1, remaining - 1):
            rank -= comb(size - candidate - 1, remaining - 1)
            candidate += 1
        indexes.append(candidate)
        candidate += 1
    return tuple(indexes)


_STATIC_EMOJI_POOL = tuple(
    "😀 😃 😄 😁 😆 😅 😂 🙂 🙃 😉 😊 🥰 😍 🤩 😋 😜 🤪 🤗 🤭 🤫 🤔 🫡 🤓 😎 🥳 🙌 👏 👍 🤝 👋 🤚 ✋ 👌 🤌 🫶 🤟 🤘 ✌ 🫰 💪 🧠 👀 💡 ✨ 🌟 ⭐ 🌈 ☀ ⛅ ☁ ❄ 🌊 🌿 🌱 🌵 🌴 🌻 🌼 🌸 🌹 🌷 🍀 🍁 🍂 🍃 🍎 🍊 🍉 🍇 🍓 🫐 ☕ 🧃 🎯 🎵 "
    "🐶 🐱 🐭 🐹 🐰 🦊 🐻 🐼 🐨 🐯 🦁 🐮 🐷 🐸 🐵 🐔 🐧 🐦 🐤 🦄 🐝 🦋 🐌 🐞 🐢 🐟 🐠 🐬 🐳 🦭 🐘 🦒 🦘 🐎 🦜 🦢 🦩 🕊 "
    "🍒 🍑 🥭 🍍 🥝 🍅 🥑 🥦 🥕 🌽 🥐 🍞 🥨 🧀 🥚 🍳 🥞 🧇 🍔 🍟 🍕 🥪 🌮 🍜 🍚 🍙 🍦 🍪 🍩 🍰 🍯 "
    "⚽ 🏀 🏈 ⚾ 🥎 🎾 🏐 🏉 🥏 🎱 🏓 🏸 🥅 ⛳ 🪁"
    .split()
)
STATIC_EMOJI_PERMUTATION_FACTOR = 104729
STATIC_EMOJI_PERMUTATION_OFFSET = 7919


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
    validate_output_slot_ids(contents, config["generation_slots"])
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
    mapped = _copy_generated_content_metadata(repaired, content)
    mapped.sequence_index = index + 1
    if request.config["generation_slots"][index].get("reply_to_message_id"):
        mapped.reply_to_sequence_index = index + 1
    decision = group_ai_chat._voice_profile_match_decision_for_item(
        repaired,
        {"summary": snapshot["account_profile"]},
        quality_item,
    )
    if int(decision["score"]) <= group_ai_chat.VOICE_PROFILE_MISMATCH_SCORE:
        return SlotGenerationResult(
            mapped,
            "voice_profile_mismatch",
            str(decision["reason"]),
            anchor_rewritten,
        )
    if reason := group_ai_chat._stance_conflict_reason(repaired, snapshot["stance_summary"]):
        return SlotGenerationResult(mapped, "stance_conflict", reason, anchor_rewritten)
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
        return SlotGenerationResult(mapped, code, code, anchor_rewritten)
    return SlotGenerationResult(
        mapped,
        voice_profile_anchor_rewritten=anchor_rewritten,
    )


def _ordered_results(request, accepted: dict, rejected: dict) -> list[SlotGenerationResult]:
    missing = SlotGenerationResult("", "quality_rejected", "all_model_stages_rejected")
    return [accepted.get(index) or rejected.get(index) or missing for index in range(len(request.batch_ids))]


__all__ = ["SlotGenerationResult", "generate_quality_results"]
