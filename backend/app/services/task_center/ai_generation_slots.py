from __future__ import annotations

from app.models import Action

from .payloads import SendMessagePayload


def generation_slot(
    action: Action,
    payload: SendMessagePayload,
    index: int,
    *,
    content_obligation_fallback_ready: bool = False,
) -> dict:
    return {
        "slot_id": payload.slot_id,
        "primary_quantity_slot_id": str(action.primary_quantity_slot_id or ""),
        "sequence_index": index,
        "cycle_turn_index": int(payload.turn_index or index),
        "account_id": action.account_id,
        "group_id": payload.group_id,
        "coverage_ledger_id": payload.coverage_ledger_id,
        "coverage_window_date": payload.coverage_window_date,
        "coverage_account_completed_before_action": payload.coverage_account_completed_before_action,
        "act_type": payload.act_type,
        "stance": payload.content_intent_stance,
        "account_profile": payload.account_profile,
        "reply_to_message_id": payload.reply_to_message_id,
        "reply_to_content": payload.reply_target_preview,
        "reply_to_sequence_index": index if payload.reply_to_message_id else None,
        "topic_direction": dict(payload.topic_direction),
        "teacher_target": dict(payload.teacher_target),
        "allocation_plan_id": payload.allocation_plan_id,
        "content_intent_id": payload.content_intent_id,
        "content_intent_config_revision": payload.content_intent_config_revision,
        "content_contract_revision": payload.content_contract_revision,
        "normal_text_ordinal": payload.normal_text_ordinal,
        "topic_rate_bps": payload.topic_rate_bps,
        "topic_budget_eligible": payload.topic_budget_eligible,
        "topic_mode": payload.topic_mode,
        "topic_capacity_reservation_id": payload.topic_capacity_reservation_id,
        "surface_scope_key": payload.surface_scope_key,
        "topic_ratio_scope_key": payload.topic_ratio_scope_key,
        "task_day": payload.content_task_day,
        "route_family": payload.route_family,
        "daily_vocabulary_theme_id": payload.daily_vocabulary_theme_id,
        "daily_vocabulary_theme_version": payload.daily_vocabulary_theme_version,
        "daily_vocabulary_theme_effective_state": payload.daily_vocabulary_theme_effective_state,
        "vocabulary_catalog_version": payload.vocabulary_catalog_version,
        "vocabulary_sample_ids": list(payload.vocabulary_sample_ids),
        "vocabulary_surface_terms": list(payload.vocabulary_surface_terms),
        "vocabulary_normalized_term_ids": list(payload.vocabulary_normalized_term_ids),
        "vocabulary_candidate_count": payload.vocabulary_candidate_count,
        "vocabulary_reservation_id": payload.vocabulary_reservation_id,
        "material_intent": payload.material_intent,
        "content_obligation_fallback_ready": content_obligation_fallback_ready,
    }


def reply_targets(batch: list[tuple[Action, SendMessagePayload]]) -> list[dict]:
    return [{
        "message_id": int(payload.reply_to_message_id or 0),
        "author": payload.reply_target_author,
        "preview": payload.reply_target_preview,
        "source": payload.reply_target_source,
    } for _action, payload in batch]
