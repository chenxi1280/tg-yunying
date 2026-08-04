from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import Action, GroupContextMessage, Task, TgAccount, TgGroup, TgGroupAccount
from app.services._common import _now

from .ai_generation_quality import fail_generation_action
from .ai_generation_timing import GENERATION_LOOKAHEAD
from .ai_generator import AI_GENERATION_UNAVAILABLE_MESSAGE, AiGenerationUnavailable
from .ai_message_memory import mark_group_ai_message_result
from .ai_quality_stats import clear_quality_blocker, quality_scope_key, record_quality_event
from .direct_check_in import (
    is_due_catch_up_check_in,
    prepare_direct_check_in,
    requires_direct_check_in,
)
from .group_ai_scope import (
    LOCAL_REPLY_TARGET_MISSING_DETAIL,
    successful_own_history_reply_facts,
    validate_group_ai_content_scope,
)
from .payloads import SendMessagePayload


CONTEXT_HISTORY_LIMIT = 50


def prepare_generation_guards(
    session: Session,
    task: Task,
    action: Action,
    *,
    account: TgAccount,
    payload: SendMessagePayload,
) -> SendMessagePayload | None:
    _validate_generation_scope(session, task, action, account=account, payload=payload)
    direct = _prepare_direct_check_in(session, action, account=account, payload=payload)
    if direct is not None:
        return direct
    require_normal_context_watermark(session, task, action, payload=payload)
    record_should_speak_shadow(session, task, action, payload=payload)
    return None


def _validate_generation_scope(
    session: Session,
    task: Task,
    action: Action,
    *,
    account: TgAccount,
    payload: SendMessagePayload,
) -> None:
    violation = validate_group_ai_content_scope(session, action, payload=payload, account_id=account.id)
    if not violation:
        return
    record_quality_event(task, action, "pre_provider_scope_reject_count", blocker=violation.code)
    fail_generation_action(action, violation.code, violation.detail, stage="content_scope")
    session.commit()
    raise AiGenerationUnavailable(violation.code)


def _prepare_direct_check_in(
    session: Session,
    action: Action,
    *,
    account: TgAccount,
    payload: SendMessagePayload,
) -> SendMessagePayload | None:
    if not requires_direct_check_in(payload):
        return None
    if payload.reply_to_message_id:
        validate_local_reply_target(session, action, payload=payload, account_id=account.id)
    try:
        prepared = prepare_direct_check_in(session, action, payload)
    except AiGenerationUnavailable as exc:
        code = str(exc) or "direct_check_in_ineligible"
        fail_generation_action(action, code, code, stage="deterministic_fallback")
        session.commit()
        raise
    session.commit()
    return prepared


def require_normal_context_watermark(
    session: Session,
    task: Task,
    action: Action,
    *,
    payload: SendMessagePayload,
) -> None:
    if payload.reply_to_message_id or payload.message_text.strip():
        return
    group = session.get(TgGroup, payload.group_id)
    snapshot = session.get(GroupContextMessage, payload.context_snapshot_message_id) if payload.context_snapshot_message_id else None
    if _normal_context_watermark_proven(group, snapshot):
        _clear_context_freshness_blocker(task, action)
        return
    _defer_unproven_context(task, action, group=group)
    session.commit()
    raise AiGenerationUnavailable("context_freshness_unproven")


def _normal_context_watermark_proven(
    group: TgGroup | None,
    snapshot: GroupContextMessage | None,
) -> bool:
    if group is None or not group.listener_enabled or group.listener_cursor_status != "contiguous":
        return False
    watermark = group.listener_last_polled_at
    if watermark is None or str(group.listener_last_error or "").strip():
        return False
    snapshot_at = (snapshot.sent_at or snapshot.created_at) if snapshot else None
    return snapshot_at is None or _naive(watermark) >= _naive(snapshot_at)


def _defer_unproven_context(task: Task, action: Action, *, group: TgGroup | None) -> None:
    retry_seconds = max(1, int(group.listener_interval_seconds or 60)) if group else 60
    action.status = "pending"
    action.scheduled_at = _now() + GENERATION_LOOKAHEAD + timedelta(seconds=retry_seconds)
    action.executed_at = None
    _clear_claim(action)
    payload = dict(action.payload or {})
    payload["ai_generation_claim_owner"] = ""
    payload["ai_generation_claim_token"] = ""
    action.payload = payload
    action.result = {
        **(action.result or {}),
        "error_code": "context_freshness_unproven",
        "generation_stage": "context_freshness",
        "generation_outcome": "pending",
    }
    record_quality_event(task, action, "context_freshness_unproven_count", blocker="context_freshness_unproven")


def _clear_claim(action: Action) -> None:
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.lease_owner = ""
    action.lease_expires_at = None


def _clear_context_freshness_blocker(task: Task, action: Action) -> None:
    result = dict(action.result or {})
    if result.get("error_code") == "context_freshness_unproven":
        result.pop("error_code", None)
        action.result = result
    blockers = dict((task.stats or {}).get("conversation_quality_active_blockers") or {})
    if blockers.get(quality_scope_key(action)) == "context_freshness_unproven":
        clear_quality_blocker(task, action)


def ready_generation_payload(session: Session, action: Action) -> SendMessagePayload:
    refreshed_action = session.get(Action, action.id)
    data = refreshed_action.payload if isinstance(refreshed_action.payload, dict) else {}
    if refreshed_action.status == "failed":
        raise AiGenerationUnavailable(str(data.get("ai_generation_status") or AI_GENERATION_UNAVAILABLE_MESSAGE))
    payload = SendMessagePayload.model_validate(data)
    if payload.ai_generation_status != "ready" or not payload.message_text.strip():
        raise AiGenerationUnavailable(payload.ai_generation_status or AI_GENERATION_UNAVAILABLE_MESSAGE)
    return payload


def record_should_speak_shadow(
    session: Session,
    task: Task,
    action: Action,
    *,
    payload: SendMessagePayload,
) -> None:
    latest_human_at = session.scalar(select(func.max(GroupContextMessage.sent_at)).where(
        GroupContextMessage.tenant_id == action.tenant_id,
        GroupContextMessage.group_id == payload.group_id,
        GroupContextMessage.is_bot.is_(False),
    ))
    question = _latest_platform_question(session, action, payload=payload)
    awaiting = bool(question and question.executed_at and (
        latest_human_at is None or _naive(question.executed_at) > _naive(latest_human_at)
    ))
    action.result = {
        **(action.result or {}),
        "should_speak_shadow_decision": "wait" if awaiting else "send",
        "should_speak_shadow_reason": "awaiting_human_response" if awaiting else "no_open_platform_question",
        "awaiting_human_response_shadow": awaiting,
        "should_speak_shadow_observed_watermark": latest_human_at.isoformat() if latest_human_at else "",
        "should_speak_shadow_next_eligible_at": None if awaiting else _now().isoformat(),
    }
    if awaiting:
        record_quality_event(task, action, "question_floor_shadow_violation_count")


def _latest_platform_question(
    session: Session,
    action: Action,
    *,
    payload: SendMessagePayload,
) -> Action | None:
    rows = session.scalars(select(Action).where(
        Action.id != action.id,
        Action.tenant_id == action.tenant_id,
        Action.task_type == "group_ai_chat",
        Action.action_type == "send_message",
        Action.status == "success",
        Action.payload["group_id"].as_integer() == payload.group_id,
    ).order_by(Action.executed_at.desc()).limit(20))
    return next((row for row in rows if str((row.payload or {}).get("message_text") or "").strip().endswith(("?", "？"))), None)


def invalidate_superseded_normal_generation(
    session: Session,
    task: Task,
    action: Action,
    *,
    payload: SendMessagePayload,
) -> SendMessagePayload:
    if payload.reply_to_message_id or not payload.message_text.strip() or payload.ai_generation_status != "ready" or not payload.ai_generation_id:
        return payload
    if is_due_catch_up_check_in(payload.model_dump(mode="json")):
        return payload
    rows = latest_context_rows(session, payload, task)
    if not rows:
        return payload
    snapshot = session.get(GroupContextMessage, payload.context_snapshot_message_id)
    latest = max(rows, key=_context_order)
    if not snapshot or _context_order(latest) <= _context_order(snapshot):
        return payload
    if _newer_human_context_count(session, task, payload, snapshot) < _context_expiration_threshold(payload):
        return payload
    if payload.ai_message_memory_id:
        mark_group_ai_message_result(
            session,
            payload.ai_message_memory_id,
            status="expired_before_send",
            action_id=action.id,
            result={"error_code": "generation_context_superseded", "latest_context_message_id": int(latest.id)},
        )
    updated = payload.model_copy(update={
        "message_text": "", "original_text": "", "ai_generation_status": "pending",
        "ai_generation_result_cache": {}, "ai_generation_tokens": 0,
        "ai_message_memory_id": "", "semantic_cluster": "",
    })
    action.payload = updated.model_dump(mode="json")
    action.result = {**(action.result or {}), "generation_stage": "context_superseded", "generation_outcome": "pending"}
    return updated


def _context_expiration_threshold(payload: SendMessagePayload) -> int:
    return max(1, int(payload.context_expire_after_messages or 0))


def _newer_human_context_count(
    session: Session,
    task: Task,
    payload: SendMessagePayload,
    snapshot: GroupContextMessage,
) -> int:
    snapshot_at = snapshot.sent_at or snapshot.created_at
    context_at = func.coalesce(GroupContextMessage.sent_at, GroupContextMessage.created_at)
    newer = or_(
        context_at > snapshot_at,
        and_(context_at == snapshot_at, GroupContextMessage.id > snapshot.id),
    )
    count = session.scalar(select(func.count(GroupContextMessage.id)).where(
        GroupContextMessage.tenant_id == task.tenant_id,
        GroupContextMessage.group_id == payload.group_id,
        GroupContextMessage.is_bot.is_(False),
        GroupContextMessage.content != "",
        newer,
    ))
    return int(count or 0)


def requeue_normal_generation_after_context_change(
    session: Session,
    task: Task,
    action: Action,
    *,
    payload: SendMessagePayload,
) -> bool:
    updated = invalidate_superseded_normal_generation(session, task, action, payload=payload)
    if updated.ai_generation_status != "pending" or updated.message_text.strip():
        return False
    updated = updated.model_copy(update={
        "ai_generation_attempt_id": "", "ai_generation_request_id": "",
        "ai_generation_claim_owner": "", "ai_generation_claim_token": "",
    })
    action.payload = updated.model_dump(mode="json")
    action.status = "pending"
    action.executed_at = None
    action.scheduled_at = _now()
    _clear_claim(action)
    result = dict(action.result or {})
    result["context_superseded_requeue_count"] = int(result.get("context_superseded_requeue_count") or 0) + 1
    result["generation_stage"] = "context_superseded_requeue"
    result["generation_outcome"] = "pending"
    action.result = result
    record_quality_event(task, action, "context_superseded_requeue_count", blocker="context_superseded_requeue")
    return True


def validate_local_reply_target(
    session: Session,
    action: Action,
    *,
    payload: SendMessagePayload,
    account_id: int,
) -> str:
    if not payload.reply_to_message_id:
        return ""
    group = session.scalar(select(TgGroup).where(TgGroup.tenant_id == action.tenant_id, TgGroup.id == payload.group_id))
    link = session.scalar(select(TgGroupAccount.id).where(
        TgGroupAccount.tenant_id == action.tenant_id,
        TgGroupAccount.group_id == payload.group_id,
        TgGroupAccount.account_id == account_id,
        TgGroupAccount.can_send.is_(True),
    ))
    if group and link and _local_reply_target_exists(session, action, payload=payload):
        return group.tg_peer_id
    fail_generation_action(
        action,
        "reply_target_missing",
        LOCAL_REPLY_TARGET_MISSING_DETAIL,
        stage="ai_reply_target",
    )
    session.commit()
    raise AiGenerationUnavailable("reply_target_missing")


def _local_reply_target_exists(
    session: Session,
    action: Action,
    *,
    payload: SendMessagePayload,
) -> bool:
    target = session.scalar(select(GroupContextMessage.id).where(
        GroupContextMessage.tenant_id == action.tenant_id,
        GroupContextMessage.group_id == payload.group_id,
        GroupContextMessage.remote_message_id == str(payload.reply_to_message_id),
    ))
    if target:
        return True
    return bool(successful_own_history_reply_facts(
        session,
        tenant_id=action.tenant_id,
        task_id=str(action.task_id or ""),
        group_id=payload.group_id,
        remote_message_id=str(payload.reply_to_message_id),
        exclude_action_id=action.id,
        limit=1,
    ))


def latest_context_rows(session: Session, payload: SendMessagePayload, task: Task) -> list[GroupContextMessage]:
    depth = min(CONTEXT_HISTORY_LIMIT, max(1, int((task.type_config or {}).get("chat_history_depth") or CONTEXT_HISTORY_LIMIT)))
    rows = session.scalars(select(GroupContextMessage).where(
        GroupContextMessage.tenant_id == task.tenant_id,
        GroupContextMessage.group_id == payload.group_id,
        GroupContextMessage.is_bot.is_(False),
        GroupContextMessage.content != "",
    ).order_by(
        func.coalesce(GroupContextMessage.sent_at, GroupContextMessage.created_at).desc(),
        GroupContextMessage.id.desc(),
    ).limit(depth))
    return list(reversed(list(rows)))


def _context_order(row: GroupContextMessage) -> tuple[datetime, int]:
    return (_naive(row.sent_at or row.created_at), int(row.id))


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


__all__ = [
    "invalidate_superseded_normal_generation",
    "latest_context_rows",
    "prepare_generation_guards",
    "ready_generation_payload",
    "record_should_speak_shadow",
    "requeue_normal_generation_after_context_change",
    "require_normal_context_watermark",
    "validate_local_reply_target",
]
