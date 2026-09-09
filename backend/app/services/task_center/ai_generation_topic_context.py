"""Prepare explicit configured-topic inputs when human context is unproven."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, GroupContextMessage, Task, TgGroup

from .ai_generator import AiGenerationUnavailable
from .ai_context_information import meaningful_context_text
from .direct_check_in import requires_direct_check_in
from .group_ai_scope import CONTENT_SCOPE_CONTRACT_VERSION
from .payloads import SendMessagePayload


TOPIC_CONTEXT_SCAN_LIMIT = 50


def prepare_topic_payload(
    session: Session,
    task: Task,
    action: Action,
    *,
    payload: SendMessagePayload,
) -> SendMessagePayload:
    if (task.type_config or {}).get("engagement_contract_version") != "unified_engagement_v1":
        return payload
    if not _ordinary_direct(action, payload):
        return payload
    if _has_foreign_reference(session, action, payload):
        return payload
    reason = _topic_reason(session, action, payload)
    if not reason:
        return payload
    topic = _configured_topic(task, payload)
    if not topic:
        raise AiGenerationUnavailable("topic_only_topic_missing")
    _require_topic_evidence(task, topic)
    updated = payload.model_copy(update={
        "ai_generation_context_mode": "topic_only",
        "ai_generation_context_reason": reason,
        "ai_generation_topic_direction": topic,
        "ai_generation_history": "",
        "ai_generation_context_count": 0,
        "anchor_message_ids": [],
        "context_message_ids": [],
        "context_snapshot_message_id": None,
        "reply_target_label": "",
        "reply_target_author": "",
        "reply_target_preview": "",
        "reply_target_source": "",
    })
    action.payload = updated.model_dump(mode="json")
    action.result = {**(action.result or {}), "generation_context_mode": "topic_only",
                     "generation_context_reason": reason}
    return updated


def _ordinary_direct(action: Action, payload: SendMessagePayload) -> bool:
    return bool(
        action.task_type == "group_ai_chat" and action.action_type == "send_message"
        and not payload.message_text.strip()
        and not payload.reply_to_message_id and not payload.interaction_opportunity_id
        and not payload.conversation_turn_claim_id
        and not requires_direct_check_in(payload)
        and _has_scope_identity(action, payload)
    )


def _has_scope_identity(action: Action, payload: SendMessagePayload) -> bool:
    return bool(
        payload.content_scope_contract_version == CONTENT_SCOPE_CONTRACT_VERSION
        and payload.content_scope_tenant_id == action.tenant_id
        and payload.content_scope_group_id == payload.group_id
        and payload.content_scope_task_id == str(action.task_id or "")
    )


def _has_foreign_reference(session: Session, action: Action, payload: SendMessagePayload) -> bool:
    ids = set(payload.context_message_ids + payload.anchor_message_ids)
    if payload.context_snapshot_message_id:
        ids.add(payload.context_snapshot_message_id)
    if not ids:
        return False
    rows = session.scalars(select(GroupContextMessage).where(GroupContextMessage.id.in_(ids)))
    return any(row.tenant_id != action.tenant_id or row.group_id != payload.group_id for row in rows)


def _topic_reason(session: Session, action: Action, payload: SendMessagePayload) -> str:
    if payload.ai_generation_context_mode == "topic_only":
        return payload.ai_generation_context_reason or "topic_only_frozen"
    group = session.get(TgGroup, payload.group_id)
    if not group or group.tenant_id != action.tenant_id:
        return ""
    if str(group.listener_last_error or "").strip():
        return "listener_error"
    if not group.listener_enabled or group.listener_cursor_status != "contiguous":
        return "listener_watermark_unproven"
    if group.listener_last_polled_at is None:
        return "listener_watermark_unproven"
    rows = session.scalars(select(GroupContextMessage).where(
        GroupContextMessage.tenant_id == action.tenant_id,
        GroupContextMessage.group_id == payload.group_id,
        GroupContextMessage.is_bot.is_(False),
        GroupContextMessage.content != "",
    ).order_by(func.coalesce(GroupContextMessage.sent_at, GroupContextMessage.created_at).desc(),
               GroupContextMessage.id.desc()).limit(TOPIC_CONTEXT_SCAN_LIMIT))
    latest = next((row for row in rows if meaningful_context_text(row.content)), None)
    if latest is None:
        return "no_human_context"
    latest_at = latest.sent_at or latest.created_at
    if group.listener_last_polled_at.replace(tzinfo=None) < latest_at.replace(tzinfo=None):
        return "listener_watermark_unproven"
    return ""


def _configured_topic(task: Task, payload: SendMessagePayload) -> dict:
    config = task.type_config or {}
    candidates = [payload.ai_generation_topic_direction, payload.topic_direction,
                  config.get("active_topic_direction"), *(config.get("topic_directions") or [])]
    return next((dict(item) for item in candidates
                 if isinstance(item, dict) and str(item.get("title") or "").strip()), {})


def _require_topic_evidence(task, topic) -> None:
    from .ai_content_job_binding import _ADULT_CONTEXT_MARKERS
    from .ai_context_information import meaningful_group_evidence
    from .ai_provider_routes import route_v2_enabled

    if route_v2_enabled(task.type_config) and not meaningful_group_evidence("", topic, _ADULT_CONTEXT_MARKERS):
        raise AiGenerationUnavailable("topic_only_topic_evidence_missing")


def prepare_topic_or_emergency(session, task, action, *, payload):
    try:
        return prepare_topic_payload(session, task, action, payload=payload)
    except AiGenerationUnavailable as exc:
        if str(exc) in {"topic_only_topic_missing", "topic_only_topic_evidence_missing"}:
            from .ai_group_emergency_pending import persist_pre_request_emergency

            persist_pre_request_emergency(session, task, action, reason=str(exc))
        raise
