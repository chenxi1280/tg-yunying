from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, AiGroupMessageMemory, TgGroup
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
) -> None:
    with session.no_autoflush:
        for action, _payload in load_generation_batch(session, request):
            fail_generation_action(action, code, detail, stage="ai_generation")
            commit_generation_action(session, request, action)


def fail_generation_action(action: Action, code: str, detail: str, *, stage: str) -> None:
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
    _release_action_coverage(action, data, code=code, detail=detail)


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
            profile_version=payload.profile_version or None,
            profile_match_score=payload.profile_match_score or None,
            profile_match_reason=payload.profile_match_reason,
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
) -> None:
    coverage_id = str(data.get("coverage_ledger_id") or "")
    session = action._sa_instance_state.session
    if not coverage_id or session is None:
        return
    release_coverage_reservation(
        session,
        coverage_id,
        action.id,
        blocker_code=code,
        blocker_detail=detail,
    )


__all__ = ["fail_generation_action", "fail_generation_batch", "store_generation_quality"]
