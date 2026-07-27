from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiAccountVoiceProfile,
    AiGroupMessageMemory,
    TaskAccountDailyCoverage,
    TgAccount,
)
from app.services._common import _now

from .ai_generator import AiGenerationUnavailable
from .ai_message_memory_text import message_identity
from .payloads import SendMessagePayload


DIRECT_CHECK_IN_TEXT = "签到"
DIRECT_CHECK_IN_SOURCE = "check_in_direct"
MASK_MISSING_CHECK_IN_SOURCE = "mask_missing_check_in"
DIRECT_GENERATION_SOURCE = "direct_check_in"
DIRECT_MEMORY_RETENTION = timedelta(days=30)


def requires_direct_check_in(payload: SendMessagePayload) -> bool:
    return bool(
        payload.coverage_ledger_id
        and not payload.reply_to_message_id
        and payload.content_source == MASK_MISSING_CHECK_IN_SOURCE
        and payload.mask_status in {
            "missing",
            "queued",
            "generating",
            "retry_wait",
            "manual_required",
        }
    )


def prepare_direct_check_in(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
) -> SendMessagePayload:
    coverage = _validated_coverage(session, action, payload)
    _validate_missing_mask_fallback(session, action, payload, coverage)
    _supersede_old_memory(session, payload)
    memory = _reserve_memory(session, action, payload, coverage)
    data = {
        **dict(action.payload),
        "message_text": DIRECT_CHECK_IN_TEXT,
        "act_type": "check_in",
        "ai_generation_status": "ready",
        "ai_generation_tokens": 0,
        "ai_generation_result_cache": {},
        "generation_source": MASK_MISSING_CHECK_IN_SOURCE,
        "content_source": MASK_MISSING_CHECK_IN_SOURCE,
        "human_quality_decision": MASK_MISSING_CHECK_IN_SOURCE,
        "quality_fallback": "",
        "fallback_reason": "",
        "ai_message_memory_id": memory.id,
    }
    action.payload = data
    action.result = {
        **(action.result or {}),
        "generation_stage": "mask_missing_check_in_ready",
        "generation_outcome": "ready",
        "voice_profile_anchor_rewritten": False,
    }
    session.flush()
    return SendMessagePayload.model_validate(data)


def _supersede_old_memory(session: Session, payload: SendMessagePayload) -> None:
    memory_id = str(payload.ai_message_memory_id or "").strip()
    if not memory_id:
        return
    memory = session.get(AiGroupMessageMemory, memory_id)
    if memory and memory.quality_decision != DIRECT_GENERATION_SOURCE:
        memory.status = "expired_before_send"
        memory.quality_decision = "superseded_by_direct_check_in"
        memory.updated_at = _now()


def direct_check_in_memory_is_valid(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
) -> bool:
    memory = session.get(AiGroupMessageMemory, payload.ai_message_memory_id)
    return bool(
        memory
        and memory.action_id == action.id
        and memory.account_id == action.account_id
        and memory.group_id == payload.group_id
        and memory.raw_text == DIRECT_CHECK_IN_TEXT
        and memory.quality_decision == MASK_MISSING_CHECK_IN_SOURCE
        and memory.content_source == MASK_MISSING_CHECK_IN_SOURCE
        and memory.mask_status == "missing"
        and memory.status in {"reserved", "claiming", "executing", "unknown_after_send", "success"}
    )


def _validated_coverage(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
) -> TaskAccountDailyCoverage:
    coverage = session.get(TaskAccountDailyCoverage, payload.coverage_ledger_id)
    valid = bool(
        coverage
        and coverage.tenant_id == action.tenant_id
        and coverage.task_id == action.task_id
        and coverage.group_id == payload.group_id
        and coverage.account_id == action.account_id
        and coverage.reserved_action_id == action.id
        and coverage.state == "reserved"
    )
    if not valid:
        raise AiGenerationUnavailable("direct_check_in_coverage_binding_invalid")
    return coverage


def _validate_missing_mask_fallback(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
    coverage: TaskAccountDailyCoverage,
) -> None:
    account = session.get(TgAccount, action.account_id)
    active_mask = session.scalar(
        select(AiAccountVoiceProfile.id).where(
            AiAccountVoiceProfile.tenant_id == action.tenant_id,
            AiAccountVoiceProfile.account_id == action.account_id,
            AiAccountVoiceProfile.status == "active",
            AiAccountVoiceProfile.quality_status == "active",
            AiAccountVoiceProfile.short_prompt_summary != "",
        ).limit(1)
    )
    expected_key = (
        f"{action.task_id}:{coverage.group_id}:{action.account_id}:"
        f"{coverage.coverage_date.isoformat()}:{MASK_MISSING_CHECK_IN_SOURCE}"
    )
    invalid_account = bool(
        not account
        or account.deleted_at is not None
        or account.account_identity != "normal"
        or account.status in {"禁用", "不可用", "Session失效", "封禁"}
    )
    if invalid_account or active_mask or payload.fallback_obligation_key != expected_key:
        raise AiGenerationUnavailable("mask_missing_check_in_ineligible")


def _reserve_memory(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
    coverage: TaskAccountDailyCoverage,
) -> AiGroupMessageMemory:
    reservation_key = f"mask-missing-check-in:{coverage.id}:{action.id}"
    existing = session.scalar(select(AiGroupMessageMemory).where(
        AiGroupMessageMemory.reservation_key == reservation_key,
    ))
    if existing:
        if existing.action_id != action.id or existing.account_id != action.account_id:
            raise AiGenerationUnavailable("direct_check_in_memory_binding_invalid")
        return existing
    now = _now()
    normalized, fingerprint, cluster, shell = message_identity(DIRECT_CHECK_IN_TEXT)
    memory = AiGroupMessageMemory(
        tenant_id=action.tenant_id,
        group_id=int(payload.group_id or 0),
        task_id=action.task_id,
        action_id=action.id,
        account_id=action.account_id,
        raw_text=DIRECT_CHECK_IN_TEXT,
        normalized_text=normalized,
        text_fingerprint=fingerprint,
        semantic_cluster=cluster,
        template_shell_key=shell,
        reservation_key=reservation_key,
        status="reserved",
        planned_at=now,
        expires_at=now + DIRECT_MEMORY_RETENTION,
        duplicate_window="mask_missing_coverage",
        quality_decision=MASK_MISSING_CHECK_IN_SOURCE,
        profile_version=int(payload.account_voice_profile_version or 0) or None,
        profile_match_score=int(payload.account_voice_profile_match_score or 0) or None,
        profile_match_reason=DIRECT_GENERATION_SOURCE,
        content_source=MASK_MISSING_CHECK_IN_SOURCE,
        mask_status="missing",
    )
    session.add(memory)
    session.flush()
    return memory


__all__ = [
    "DIRECT_CHECK_IN_SOURCE",
    "DIRECT_CHECK_IN_TEXT",
    "MASK_MISSING_CHECK_IN_SOURCE",
    "direct_check_in_memory_is_valid",
    "prepare_direct_check_in",
    "requires_direct_check_in",
]
