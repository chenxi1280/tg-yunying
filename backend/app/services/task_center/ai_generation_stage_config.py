from __future__ import annotations


AI_GROUP_GENERATION_ATTEMPTS_PER_MODEL = 3


def stage_config(config: dict, indexes: list[int], stage: str) -> dict:
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


def fallback_stages(config: dict) -> tuple[str, ...]:
    if bool(config.get("require_mimo_draft")):
        return ("direct_mimo",)
    if str(config.get("ai_model") or "").strip():
        return ("direct_configured_model",)
    stages = ["primary_default"] * AI_GROUP_GENERATION_ATTEMPTS_PER_MODEL
    if bool(config.get("_ai_group_model_fallback_enabled", True)):
        stages.extend(["fallback_m25"] * AI_GROUP_GENERATION_ATTEMPTS_PER_MODEL)
    return tuple(stages)




def two_stage_plan_slots(request) -> list[dict]:
    slots: list[dict] = []
    for index, raw in enumerate(request.config.get("generation_slots") or []):
        item = dict(raw)
        if request.is_reply and request.reply_targets:
            item["reply_preview"] = str((request.reply_targets[index] or {}).get("preview") or "")
        slots.append(item)
    return slots


__all__ = ["fallback_stages", "stage_config", "two_stage_plan_slots"]
