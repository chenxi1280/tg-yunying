from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Action
from app.services._common import _now

from .ai_message_memory import mark_group_ai_message_result


RESET_FIELDS = {
    "message_text": "",
    "original_text": "",
    "ai_generation_status": "pending",
    "ai_generation_result_cache": {},
    "ai_generation_tokens": 0,
    "ai_generation_attempt_id": "",
    "ai_generation_request_id": "",
    "ai_generation_claim_owner": "",
    "ai_generation_claim_token": "",
    "ai_message_memory_id": "",
    "semantic_cluster": "",
    "account_role": "",
    "account_memory": "",
    "account_profile": "",
    "stance_summary": "",
    "content_source": "",
    "quality_fallback": "",
    "fallback_reason": "",
    "mask_status": "",
    "account_mask_id": "",
    "account_mask_version": 0,
    "account_mask_snapshot_hash": "",
    "account_mask_summary": "",
    "account_voice_profile_version": 0,
    "account_voice_profile_summary": "",
    "voice_profile_contract_version": "",
}


def requeue_after_speaker_rebind(
    session: Session,
    action: Action,
    *,
    account_id: int,
    previous_account_id: int,
    reason: str,
) -> None:
    payload = dict(action.payload or {})
    memory_id = str(payload.get("ai_message_memory_id") or "")
    if memory_id:
        mark_group_ai_message_result(
            session,
            memory_id,
            status="expired_before_send",
            action_id=action.id,
            result={"error_code": "speaker_rebound", "replacement_account_id": account_id},
        )
    action.account_id = account_id
    action.payload = {
        **payload,
        **RESET_FIELDS,
        "account_id": account_id,
        "previous_speaker_account_id": previous_account_id,
        "speaker_selection_reason": reason,
    }
    action.status = "pending"
    action.scheduled_at = _now()
    action.executed_at = None
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.lease_owner = ""
    action.lease_expires_at = None
    action.result = {
        **(action.result or {}),
        "generation_stage": "speaker_rebind_requeue",
        "generation_outcome": "pending",
    }


__all__ = ["requeue_after_speaker_rebind"]
