from __future__ import annotations

from app.models import Action

from .payloads import SendMessagePayload


def generation_slot(action: Action, payload: SendMessagePayload, index: int) -> dict:
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
        "account_profile": payload.account_profile,
        "reply_to_message_id": payload.reply_to_message_id,
        "reply_to_content": payload.reply_target_preview,
        "reply_to_sequence_index": index if payload.reply_to_message_id else None,
        "topic_direction": dict(payload.topic_direction),
        "teacher_target": dict(payload.teacher_target),
    }


def reply_targets(batch: list[tuple[Action, SendMessagePayload]]) -> list[dict]:
    return [{
        "message_id": int(payload.reply_to_message_id or 0),
        "author": payload.reply_target_author,
        "preview": payload.reply_target_preview,
        "source": payload.reply_target_source,
    } for _action, payload in batch]
