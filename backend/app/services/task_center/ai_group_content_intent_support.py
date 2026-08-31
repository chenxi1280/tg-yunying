from __future__ import annotations

from typing import Any
import json

from app.models import AiGroupContentAllocationPlan, AiGroupContentIntent, Task

from .ai_group_content_history import ContentHistoryMessage

from .ai_group_vocabulary_sampling import (
    VocabularySamplingResult,
    sample_vocabulary_for_slot,
)


class GenericWarmupQuestionWait(RuntimeError):
    """The only legal warmup act cannot fit the remote question mix window."""


def assert_generic_warmup_question_capacity(
    quality_items: list[dict],
    recent_intents: list[AiGroupContentIntent],
) -> None:
    recent_acts = [intent.act_type for intent in recent_intents[:9]]
    for item in quality_items:
        proposed = str(
            (item.get("slot") or {}).get("act_type") or item.get("act_type") or ""
        )
        if proposed != "question":
            raise GenericWarmupQuestionWait("generic_warmup_must_be_question")
        if recent_acts[:2] == ["question", "question"]:
            raise GenericWarmupQuestionWait("generic_warmup_question_mix_wait")
        if recent_acts[:9].count("question") >= 4:
            raise GenericWarmupQuestionWait("generic_warmup_question_mix_wait")
        recent_acts.insert(0, "question")


def sample_vocabulary_for_intent(
    task: Task,
    plan: AiGroupContentAllocationPlan,
    *,
    ordinal: int,
    slot: dict,
    recent_intents: list[AiGroupContentIntent],
    recent_vocabulary_messages: list[ContentHistoryMessage],
    is_generic_warmup: bool,
) -> VocabularySamplingResult:
    suppression = vocabulary_suppression_reason(
        task.type_config or {}, persona=str(slot.get("persona") or "")
    )
    if suppression:
        return VocabularySamplingResult((), (), suppression, 0)
    recent_id_messages = [item.sample_ids for item in recent_vocabulary_messages]
    term_counts: dict[str, int] = {}
    for item in recent_vocabulary_messages:
        for term in item.term_ids:
            term_counts[str(term)] = term_counts.get(str(term), 0) + 1
    route = "adult_service" if plan.route_family == "adult" else "general_chat"
    return sample_vocabulary_for_slot(
        surface_scope_key=plan.surface_scope_key,
        task_day=plan.task_day,
        allocation_plan_id=plan.id,
        plan_unit_ordinal=ordinal,
        daily_vocabulary_theme_id=plan.daily_vocabulary_theme_id,
        route=route,
        route_family=plan.route_family,
        act_type=str(slot.get("act_type") or "") or None,
        stance=str(slot.get("stance") or "") or None,
        topic_mode=str(slot.get("topic_mode") or "group_free_chat"),
        evidence_text=str(slot.get("vocabulary_evidence_text") or ""),
        is_generic_warmup=is_generic_warmup,
        recent_used_ids_by_message=recent_id_messages,
        recent_term_counts=term_counts,
    )


def vocabulary_evidence_text(
    item: dict,
    slot: dict,
    topic_direction: dict | None,
) -> str:
    values = [
        str((item.get("reply_target") or {}).get("content") or ""),
        str(slot.get("reply_to_content") or ""),
        json.dumps(topic_direction or {}, ensure_ascii=False, sort_keys=True),
        json.dumps(slot.get("teacher_target") or {}, ensure_ascii=False, sort_keys=True),
        str(item.get("material_text") or ""),
    ]
    return "\n".join(value for value in values if value)


def sampled_vocabulary_values(sampled, ordinal: int) -> tuple[list[str], list[str]]:
    surface_terms = [
        unit.surface_terms[ordinal % len(unit.surface_terms)]
        for unit in sampled.sample_units
    ]
    normalized = sorted(
        {term for unit in sampled.sample_units for term in unit.normalized_term_ids}
    )
    return surface_terms, normalized


def vocabulary_suppression_reason(config: dict, *, persona: str = "") -> str:
    if str(config.get("system_prompt_override") or "").strip():
        return "suppressed_by_override"
    if str(config.get("tone") or "auto") == "professional":
        return "suppressed_by_tone"
    if persona.strip():
        return "suppressed_by_persona"
    return ""


def constrained_act_type(
    slot: dict,
    item: dict,
    recent_intents: list[AiGroupContentIntent],
) -> str:
    proposed = str(slot.get("act_type") or item.get("act_type") or "")
    if proposed != "question":
        return proposed
    recent_acts = [intent.act_type for intent in recent_intents[:9]]
    if recent_acts[:2] == ["question", "question"]:
        return "short_react"
    if recent_acts.count("question") >= 4:
        return "short_react"
    return proposed


def choose_weighted_topic_direction(
    topics: list[dict[str, Any]],
    recent_intents: list[AiGroupContentIntent],
) -> dict[str, Any] | None:
    if not topics:
        return None
    usage: dict[str, int] = {}
    for intent in recent_intents:
        title = str((intent.topic_direction_snapshot or {}).get("title") or "").strip()
        if title:
            usage[title] = usage.get(title, 0) + 1
    ranked = enumerate(topics)
    _index, chosen = min(
        ranked,
        key=lambda pair: (
            (usage.get(str(pair[1].get("title") or "").strip(), 0) + 1)
            / max(float(pair[1].get("weight") or 1), 0.01),
            pair[0],
        ),
    )
    return dict(chosen)


def with_content_intent(
    item: dict,
    plan: AiGroupContentAllocationPlan,
    intent: AiGroupContentIntent,
) -> dict:
    slot = {
        **dict(item.get("slot") or {}),
        "allocation_plan_id": plan.id,
        "content_intent_id": intent.id,
        "content_intent_config_revision": intent.config_revision,
        "content_intent_config_snapshot_hash": intent.config_snapshot_hash,
        "content_intent_task_lifecycle_epoch": intent.task_lifecycle_epoch,
        "content_intent_target_reference_revision": intent.target_reference_revision,
        "content_contract_revision": "ai_group_content_v1_2",
        "normal_text_ordinal": intent.normal_text_ordinal,
        "relation_kind": intent.relation_kind,
        "act_type": intent.act_type,
        "stance": intent.stance,
        "topic_rate_bps": plan.topic_rate_bps,
        "topic_budget_eligible": bool(intent.topic_budget_eligible),
        "topic_mode": intent.topic_mode,
        "teacher_target": dict(intent.teacher_target_snapshot),
        "topic_capacity_reservation_id": intent.topic_capacity_reservation_id,
        "daily_vocabulary_theme_id": intent.daily_vocabulary_theme_id,
        "daily_vocabulary_theme_version": plan.daily_vocabulary_theme_version,
        "daily_vocabulary_theme_effective_state": intent.daily_vocabulary_theme_effective_state,
        "vocabulary_catalog_version": intent.vocabulary_catalog_version,
        "vocabulary_sample_ids": list(intent.vocabulary_sample_ids),
        "vocabulary_surface_terms": list(intent.vocabulary_surface_terms),
        "vocabulary_normalized_term_ids": list(intent.vocabulary_normalized_term_ids),
        "vocabulary_candidate_count": intent.vocabulary_candidate_count,
        "vocabulary_reservation_id": intent.vocabulary_reservation_id,
        "surface_scope_key": plan.surface_scope_key,
        "topic_ratio_scope_key": f"task:{plan.task_id}:day:{plan.task_day.isoformat()}:target:{plan.target_operation_target_id}",
        "task_day": plan.task_day.isoformat(),
        "route_family": plan.route_family,
    }
    if intent.topic_direction_snapshot:
        slot["topic_direction"] = dict(intent.topic_direction_snapshot)
    else:
        slot.pop("topic_direction", None)
    return {**item, "slot": slot}


__all__ = [
    "GenericWarmupQuestionWait",
    "assert_generic_warmup_question_capacity",
    "choose_weighted_topic_direction",
    "constrained_act_type",
    "sample_vocabulary_for_intent",
    "sampled_vocabulary_values",
    "vocabulary_evidence_text",
    "vocabulary_suppression_reason",
    "with_content_intent",
]
