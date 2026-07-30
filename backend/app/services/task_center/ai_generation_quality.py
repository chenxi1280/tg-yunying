from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiCoverageVariationIntent,
    AiGenerationContractAudit,
    AiGroupMessageMemory,
    ContentMixContract,
    ContentMixCycle,
    ContentMixObligation,
    TgGroup,
)
from app.services._common import _now
from app.services.content_filters import filter_outbound_content
from app.services.material_rules import MaterialRuleResult, select_material_for_policy

from .ai_generation_commit import commit_generation_action, load_generation_batch
from .ai_message_memory import (
    DuplicateMemoryBatch,
    DuplicateMessageReservation,
    mark_group_ai_message_result,
    normalize_group_ai_text,
    reserve_group_ai_message,
)
from .daily_coverage import release_coverage_reservation
from .daily_coverage import block_generation_contract_coverage
from .payloads import SendMessagePayload
from .policies import validate_group_send_policy


def store_generation_quality(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
    *,
    data: dict,
    duplicate_batch: DuplicateMemoryBatch | None = None,
) -> bool:
    if not _content_policy_allows(session, action, payload, data=data):
        return False
    if not _apply_material_policy(session, action, payload, data=data):
        return False
    return _attach_message_memory(
        session, action, payload, data=data, duplicate_batch=duplicate_batch,
    )


def fail_generation_batch(
    session: Session,
    request,
    code: str,
    *,
    detail: str,
    mapping_error: Exception | None = None,
) -> None:
    is_contract_failure = code in _GENERATION_CONTRACT_CODES
    if is_contract_failure:
        _record_generation_contract_audit(session, request, code, detail, mapping_error)
    with session.no_autoflush:
        for action, _payload in load_generation_batch(session, request):
            fail_generation_action(
                action,
                code,
                detail,
                stage="generation_contract" if is_contract_failure else "ai_generation",
                generation_contract=is_contract_failure,
            )
            commit_generation_action(session, request, action)


def fail_generation_action(
    action: Action,
    code: str,
    detail: str,
    *,
    stage: str,
    generation_contract: bool = False,
) -> None:
    data = dict(action.payload or {})
    data["ai_generation_status"] = code
    action.payload = data
    action.status = "failed"
    action.executed_at = _now()
    action.result = {
        **(action.result or {}),
        "success": False,
        "error_code": code,
        "error_message": detail,
        "auto_check": "拦截",
        "validation_stage": stage,
        "generation_stage": stage,
        "generation_outcome": code,
        "generation_category": _generation_category(code, stage),
    }
    _release_action_coverage(
        action,
        data,
        code=code,
        detail=detail,
        generation_contract=generation_contract,
    )


def _generation_category(code: str, stage: str) -> str:
    if code in {"reply_target_missing", "reply_target_stale"}:
        return "reply_target_invalid"
    if stage in {
        "ai_generation_quality", "content_policy", "ai_message_memory", "material_policy",
    }:
        return "quality_rejected"
    return "generation_failed"


def _content_policy_allows(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
    *,
    data: dict,
) -> bool:
    group = session.get(TgGroup, payload.group_id) if payload.group_id else None
    content = str(data.get("message_text") or "")
    if not group:
        fail_generation_action(action, "peer_invalid", "目标群不存在", stage="content_policy")
        return False
    failure_type, detail = validate_group_send_policy(
        session,
        tenant_id=action.tenant_id,
        group=group,
        content=content,
        review_approved=payload.review_approved,
    )
    filtered = filter_outbound_content(session, tenant_id=action.tenant_id, group=group, content=content)
    if failure_type or not filtered.ok:
        fail_generation_action(
            action,
            failure_type or "content_rejected",
            detail or filtered.reason,
            stage="content_policy",
        )
        return False
    data["message_text"] = filtered.content
    return True


def _apply_material_policy(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
    *,
    data: dict,
) -> bool:
    policy = payload.rule_trace.get("material_policy") or {}
    result = select_material_for_policy(
        session,
        action.tenant_id,
        policy,
        context_key=f"{payload.cycle_id}:{payload.turn_index}:{data.get('message_text') or ''}",
        default_caption="",
        material_intent=str(data.get("material_intent") or ""),
    )
    data["rule_trace"] = _material_rule_trace(payload.rule_trace, result)
    data["media_segments"] = [result.segment] if result.ok and result.segment else []
    _freeze_selector_plan(session, action, data=data, result=result)
    if not result.failure_reason or result.fallback != "skip":
        return True
    action.payload = data
    fail_generation_action(
        action,
        "material_unavailable",
        result.failure_reason,
        stage="material_policy",
    )
    return False


def _freeze_selector_plan(
    session: Session,
    action: Action,
    *,
    data: dict,
    result: MaterialRuleResult,
) -> None:
    material_kind = _selected_material_kind(result)
    normal_emoji = _normal_text_contains_emoji(data, material_kind)
    data["planned_material_kind"] = material_kind
    data["planned_normal_text_emoji"] = "yes" if normal_emoji else "no"
    if material_kind != "none":
        _ensure_selector_obligation(session, action, material_kind)
    if normal_emoji:
        _ensure_selector_obligation(session, action, "normal_text_emoji")


def _selected_material_kind(result: MaterialRuleResult) -> str:
    if not result.ok or not result.segment:
        return "none"
    asset_kind = str(result.segment.get("emoji_asset_kind") or "")
    material_type = str(result.segment.get("segment_type") or "")
    if asset_kind == "custom_emoji":
        return "custom_emoji"
    if asset_kind == "sticker" or material_type == "贴纸":
        return "sticker"
    return "image"


def _normal_text_contains_emoji(data: dict, material_kind: str) -> bool:
    if material_kind != "none":
        return False
    if str(data.get("content_source") or "normal") != "normal":
        return False
    return bool(re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", str(data.get("message_text") or "")))


def _ensure_selector_obligation(
    session: Session,
    action: Action,
    obligation_kind: str,
) -> None:
    contract = _content_mix_contract_for_action(session, action)
    if contract is None or not action.content_mix_cycle_slot_id:
        return
    existing = session.scalar(select(ContentMixObligation).where(
        ContentMixObligation.content_mix_contract_id == contract.id,
        ContentMixObligation.obligation_kind == obligation_kind,
        ContentMixObligation.assigned_cycle_slot_id == action.content_mix_cycle_slot_id,
    ))
    if existing is not None:
        if not existing.assigned_action_id:
            existing.assigned_action_id = action.id
        return
    ordinal = int(session.scalar(select(func.max(ContentMixObligation.obligation_ordinal)).where(
        ContentMixObligation.content_mix_contract_id == contract.id,
        ContentMixObligation.obligation_source == "selector_plan",
        ContentMixObligation.obligation_kind == obligation_kind,
    )) or 0) + 1
    session.add(ContentMixObligation(
        tenant_id=action.tenant_id,
        content_mix_contract_id=contract.id,
        content_mix_scope_key=contract.content_mix_scope_key,
        obligation_source="selector_plan",
        obligation_kind=obligation_kind,
        obligation_ordinal=ordinal,
        assigned_cycle_slot_id=action.content_mix_cycle_slot_id,
        assigned_action_id=action.id,
        assignment_version=1,
        required_count=1,
        planned_count=1,
    ))


def _content_mix_contract_for_action(
    session: Session,
    action: Action,
) -> ContentMixContract | None:
    cycle_slot_id = action.content_mix_cycle_slot_id
    if not cycle_slot_id:
        return None
    from app.models import ContentMixCycleSlot

    cycle_slot = session.get(ContentMixCycleSlot, cycle_slot_id)
    cycle = session.get(ContentMixCycle, cycle_slot.cycle_id) if cycle_slot else None
    if cycle is None:
        return None
    scope_key = (
        f"ai:{cycle.task_id}:{cycle.target_operation_target_id}:"
        f"{cycle.id}:{cycle.config_revision}"
    )
    return session.scalar(select(ContentMixContract).where(
        ContentMixContract.content_mix_scope_key == scope_key,
        ContentMixContract.content_contract_version == cycle.config_revision,
    ))


def _material_rule_trace(source: dict, result: MaterialRuleResult) -> dict:
    return {
        **source,
        "material_action": result.action,
        "material_intent": result.material_intent,
        "material_matched_tags": result.matched_tags or [],
        "material_candidate_count": int(result.candidate_count or 0),
        "material_id": result.selected.id if result.selected else None,
        "material_failure_reason": result.failure_reason,
    }


def _attach_message_memory(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
    *,
    data: dict,
    duplicate_batch: DuplicateMemoryBatch | None = None,
) -> bool:
    memory = _reusable_message_memory(session, action, data=data)
    if memory:
        _attach_memory_payload(data, memory)
        return True
    try:
        memory = reserve_group_ai_message(
            session,
            tenant_id=action.tenant_id,
            group_id=int(payload.group_id or 0),
            task_id=action.task_id,
            account_id=action.account_id,
            raw_text=str(data.get("message_text") or ""),
            topic_direction=str(payload.topic_direction.get("title") or ""),
            teacher_target=str(payload.teacher_target.get("name") or ""),
            profile_version=payload.account_mask_version or None,
            profile_match_score=payload.account_mask_match_score or None,
            profile_match_reason=payload.account_mask_match_reason,
            account_mask_id=payload.account_mask_id,
            account_mask_version=payload.account_mask_version or None,
            mask_contract_version=payload.voice_profile_contract_version,
            mask_snapshot_hash=payload.account_mask_snapshot_hash,
            mask_status=payload.mask_status or "active",
            content_source=str(data.get("content_source") or payload.content_source or "account_mask"),
            duplicate_batch=duplicate_batch,
        )
    except DuplicateMessageReservation as exc:
        _mark_duplicate(action, data, exc)
        return False
    mark_group_ai_message_result(session, memory.id, status="reserved", action_id=action.id)
    _attach_memory_payload(data, memory)
    return True


def _reusable_message_memory(
    session: Session,
    action: Action,
    *,
    data: dict,
) -> AiGroupMessageMemory | None:
    content = str(data.get("message_text") or "")
    memory = session.scalar(select(AiGroupMessageMemory).where(
        AiGroupMessageMemory.action_id == action.id,
        AiGroupMessageMemory.status == "reserved",
    ))
    if memory and memory.normalized_text == normalize_group_ai_text(content):
        return memory
    return None


def _attach_memory_payload(data: dict, memory: AiGroupMessageMemory) -> None:
    data["ai_message_memory_id"] = memory.id
    data["semantic_cluster"] = data.get("semantic_cluster") or memory.semantic_cluster


def _mark_duplicate(action: Action, data: dict, exc: DuplicateMessageReservation) -> None:
    data.update({
        "ai_generation_status": "duplicate_rejected",
        "quality_skip_reason": "duplicate_message",
        "duplicate_risk": exc.duplicate_window,
    })
    action.payload = data
    fail_generation_action(
        action,
        "duplicate_message",
        f"AI 活群生成内容重复：{exc.duplicate_window}",
        stage="ai_message_memory",
    )
    action.result = {**(action.result or {}), "duplicate_reference_id": exc.reference_id}


def _release_action_coverage(
    action: Action,
    data: dict,
    *,
    code: str,
    detail: str,
    generation_contract: bool = False,
) -> None:
    coverage_id = str(data.get("coverage_ledger_id") or "")
    session = action._sa_instance_state.session
    if not coverage_id or session is None:
        return
    _record_variation_outcome(session, action, code)
    if generation_contract:
        block_generation_contract_coverage(
            session,
            coverage_id,
            action.id,
            blocker_code=code,
            blocker_detail=detail,
        )
        return
    release_coverage_reservation(session, coverage_id, action.id, blocker_code=code, blocker_detail=detail)


def _record_variation_outcome(session: Session, action: Action, code: str) -> None:
    if code != "duplicate_message":
        return
    intent = session.scalar(select(AiCoverageVariationIntent).where(
        AiCoverageVariationIntent.action_id == action.id,
    ))
    if intent is not None:
        intent.outcome = "quality_rejected"


_GENERATION_CONTRACT_CODES = frozenset({
    "ai_generation_output_count_mismatch",
    "ai_generation_slot_mapping_invalid",
    "ai_generation_slot_mapping_mismatch",
    "ai_generation_output_sequence_duplicate",
    "ai_generation_output_sequence_mismatch",
    "ai_generation_reply_sequence_mismatch",
    "ai_generation_reply_sequence_unexpected",
    "ai_generation_output_empty",
})


def _record_generation_contract_audit(
    session: Session,
    request,
    code: str,
    detail: str,
    mapping_error: Exception | None,
) -> None:
    attempt_id = str(getattr(request, "attempt_id", "") or "")
    if not attempt_id:
        return
    existing = session.scalar(select(AiGenerationContractAudit).where(
        AiGenerationContractAudit.generation_attempt_id == attempt_id,
    ))
    if existing is not None:
        return
    slots = list(getattr(request, "config", {}).get("generation_slots") or [])
    batch_ids = list(getattr(request, "batch_ids", []) or [])
    first_action = session.get(Action, batch_ids[0]) if batch_ids else None
    payload = first_action.payload if first_action and isinstance(first_action.payload, dict) else {}
    config = getattr(request, "config", {}) or {}
    session.add(AiGenerationContractAudit(
        tenant_id=int(getattr(request, "tenant_id", 0) or 0),
        task_id=str(getattr(request, "task_id", "") or ""),
        generation_attempt_id=attempt_id,
        request_id=str(payload.get("ai_generation_request_id") or ""),
        provider_id=str(config.get("provider_id") or config.get("provider") or ""),
        model_id=str(config.get("actual_model") or config.get("requested_model") or ""),
        prompt_contract_version=str(config.get("prompt_contract_version") or ""),
        parser_version=str(config.get("parser_version") or ""),
        expected_slot_count=int(
            getattr(mapping_error, "expected_slot_count", None) or len(slots)
        ),
        received_slot_count=int(getattr(mapping_error, "received_slot_count", 0) or 0),
        slot_summary=_contract_slot_summary(slots),
        error_code=code,
        restricted_response_summary=str(detail or code)[:500],
    ))


def _contract_slot_summary(slots: list[dict]) -> dict:
    return {
        "slot_ids": [str(slot.get("slot_id") or "") for slot in slots],
        "sequence_indexes": [index for index, _slot in enumerate(slots, 1)],
    }


__all__ = ["fail_generation_action", "fail_generation_batch", "store_generation_quality"]
