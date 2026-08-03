from __future__ import annotations

from collections.abc import Mapping
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
DUE_CATCH_UP_CHECK_IN_SOURCE = "due_catch_up_check_in"
DUE_CATCH_UP_CHECK_IN_REASON = "due_catch_up_provider_budget_exhausted"
DIRECT_GENERATION_SOURCE = "direct_check_in"
DIRECT_MEMORY_RETENTION = timedelta(days=30)
DIRECT_CHECK_IN_DEDUPE_WINDOW = timedelta(days=10)


def requires_direct_check_in(payload: SendMessagePayload) -> bool:
    return bool(
        payload.coverage_ledger_id
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
    _validate_missing_mask_fallback(
        session,
        action,
        payload,
        coverage=coverage,
    )
    _supersede_old_memory(session, payload)
    memory = _reserve_memory(session, action, payload, coverage=coverage)
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
    if memory and memory.quality_decision not in {
        DIRECT_GENERATION_SOURCE,
        MASK_MISSING_CHECK_IN_SOURCE,
    }:
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


def is_due_catch_up_check_in(data: Mapping[str, object]) -> bool:
    return bool(
        str(data.get("message_text") or "").strip() == DIRECT_CHECK_IN_TEXT
        and data.get("content_source") == DUE_CATCH_UP_CHECK_IN_SOURCE
        and data.get("generation_source") == "static_safe_fallback"
        and data.get("quality_fallback") == "check_in_fallback"
        and data.get("fallback_reason") == DUE_CATCH_UP_CHECK_IN_REASON
        and str(data.get("coverage_ledger_id") or "").strip()
        and str(data.get("daily_group_target_id") or "").strip()
        and str(data.get("primary_quantity_slot_id") or "").strip()
        and not data.get("reply_to_message_id")
        and not str(data.get("material_intent") or "").strip()
    )


def reserve_due_catch_up_check_in_memory(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
    *,
    data: dict,
) -> AiGroupMessageMemory:
    if not is_due_catch_up_check_in(data):
        raise AiGenerationUnavailable("due_catch_up_check_in_contract_invalid")
    coverage = _validated_coverage(session, action, payload)
    reservation_key = f"due-catch-up-check-in:{coverage.id}:{action.id}"
    existing = session.scalar(select(AiGroupMessageMemory).where(
        AiGroupMessageMemory.reservation_key == reservation_key,
    ))
    if existing:
        if not _due_catch_up_memory_matches(existing, action, payload, data):
            raise AiGenerationUnavailable("due_catch_up_check_in_memory_binding_invalid")
        return existing
    memory = _new_due_catch_up_memory(action, payload, data, reservation_key)
    session.add(memory)
    session.flush()
    return memory


def due_catch_up_check_in_memory_is_valid(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
) -> bool:
    if not is_due_catch_up_check_in(payload.model_dump(mode="json")):
        return False
    memory = session.get(AiGroupMessageMemory, payload.ai_message_memory_id)
    return bool(memory and _due_catch_up_memory_matches(
        memory,
        action,
        payload,
        payload.model_dump(mode="json"),
    ))


def _new_due_catch_up_memory(
    action: Action,
    payload: SendMessagePayload,
    data: dict,
    reservation_key: str,
) -> AiGroupMessageMemory:
    now = _now()
    normalized, fingerprint, cluster, shell = message_identity(DIRECT_CHECK_IN_TEXT)
    return AiGroupMessageMemory(
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
        duplicate_window="due_catch_up_quantity_slot",
        quality_decision=DUE_CATCH_UP_CHECK_IN_SOURCE,
        account_mask_id=str(data.get("account_mask_id") or ""),
        account_mask_version=int(data.get("account_mask_version") or 0) or None,
        mask_contract_version=str(data.get("voice_profile_contract_version") or ""),
        mask_snapshot_hash=str(data.get("account_mask_snapshot_hash") or ""),
        mask_status=str(data.get("mask_status") or ""),
        content_source=DUE_CATCH_UP_CHECK_IN_SOURCE,
        result={
            "fallback_reason": DUE_CATCH_UP_CHECK_IN_REASON,
            "coverage_ledger_id": payload.coverage_ledger_id,
            "daily_group_target_id": payload.daily_group_target_id,
            "primary_quantity_slot_id": payload.primary_quantity_slot_id,
        },
    )


def _due_catch_up_memory_matches(
    memory: AiGroupMessageMemory,
    action: Action,
    payload: SendMessagePayload,
    data: Mapping[str, object],
) -> bool:
    result = memory.result if isinstance(memory.result, dict) else {}
    return bool(
        memory.action_id == action.id
        and memory.account_id == action.account_id
        and memory.group_id == payload.group_id
        and memory.raw_text == DIRECT_CHECK_IN_TEXT
        and memory.quality_decision == DUE_CATCH_UP_CHECK_IN_SOURCE
        and memory.content_source == DUE_CATCH_UP_CHECK_IN_SOURCE
        and memory.status in {"reserved", "claiming", "executing", "unknown_after_send", "success"}
        and memory.account_mask_id == str(data.get("account_mask_id") or "")
        and memory.account_mask_version == int(data.get("account_mask_version") or 0)
        and memory.mask_contract_version == str(data.get("voice_profile_contract_version") or "")
        and memory.mask_snapshot_hash == str(data.get("account_mask_snapshot_hash") or "")
        and memory.mask_status == str(data.get("mask_status") or "")
        and result.get("fallback_reason") == DUE_CATCH_UP_CHECK_IN_REASON
        and result.get("coverage_ledger_id") == payload.coverage_ledger_id
        and result.get("daily_group_target_id") == payload.daily_group_target_id
        and result.get("primary_quantity_slot_id") == payload.primary_quantity_slot_id
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
    *,
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
    if (
        invalid_account
        or active_mask
        or payload.fallback_obligation_key != expected_key
    ):
        raise AiGenerationUnavailable("mask_missing_check_in_ineligible")
    if _recent_check_in_exists(session, action):
        raise AiGenerationUnavailable("direct_check_in_10d_duplicate")


def _recent_check_in_exists(session: Session, action: Action) -> bool:
    cutoff = _now() - DIRECT_CHECK_IN_DEDUPE_WINDOW
    return bool(session.scalar(select(AiGroupMessageMemory.id).where(
        AiGroupMessageMemory.tenant_id == action.tenant_id,
        AiGroupMessageMemory.account_id == action.account_id,
        AiGroupMessageMemory.action_id != action.id,
        AiGroupMessageMemory.raw_text == DIRECT_CHECK_IN_TEXT,
        AiGroupMessageMemory.planned_at >= cutoff,
        AiGroupMessageMemory.status.in_([
            "reserved",
            "claiming",
            "executing",
            "unknown_after_send",
            "success",
        ]),
    ).limit(1)))


def _reserve_memory(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
    *,
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
    "DUE_CATCH_UP_CHECK_IN_REASON",
    "DUE_CATCH_UP_CHECK_IN_SOURCE",
    "DIRECT_CHECK_IN_SOURCE",
    "DIRECT_CHECK_IN_TEXT",
    "MASK_MISSING_CHECK_IN_SOURCE",
    "direct_check_in_memory_is_valid",
    "due_catch_up_check_in_memory_is_valid",
    "is_due_catch_up_check_in",
    "prepare_direct_check_in",
    "reserve_due_catch_up_check_in_memory",
    "requires_direct_check_in",
]
