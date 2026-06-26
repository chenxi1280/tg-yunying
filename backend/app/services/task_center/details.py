from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, ChannelMessage, ExecutionAttempt, GroupContextMessage, OperationTarget, Task, TgAccount, TgGroup, VerificationTask

from .executors.common import quantity_jitter_bounds
from .executors.group_ai_chat import account_profile_summaries
from .executors.group_relay import relay_source_filter_reason
from .fingerprints import content_fingerprint
from .membership_recovery import classify_membership_recovery
from .account_pool import task_account_coverage
from app.services.task_runtime_stage import derive_task_runtime_stage


def _task_payload(session: Session, task: Task, actions: list[Action] | None = None, *, include_detail_search: bool = True) -> dict[str, Any]:
    target_summary = _target_summary(session, task)
    stats = _stats_with_account_coverage(session, task, task.stats or {})
    search_parts = [
        task.id,
        task.name,
        task.type,
        task.status,
        task.last_error,
        target_summary,
    ]
    if include_detail_search:
        search_parts.append(_task_config_search_text(session, task))
    if actions:
        for action in actions:
            payload = action.payload or {}
            search_parts.extend(
                [
                    action.action_type,
                    action.status,
                    str(payload.get("target_display") or ""),
                    str(payload.get("message_content") or ""),
                    str(payload.get("message_text") or ""),
                    str(payload.get("comment_text") or ""),
                ]
            )
    return {
        "id": task.id,
        "tenant_id": task.tenant_id,
        "name": task.name,
        "type": task.type,
        "status": task.status,
        "priority": task.priority,
        "timezone": task.timezone,
        "scheduled_start": task.scheduled_start,
        "scheduled_end": task.scheduled_end,
        "max_duration_hours": task.max_duration_hours,
        "next_run_at": task.next_run_at,
        "last_error": task.last_error,
        "account_config": task.account_config or {},
        "pacing_config": task.pacing_config or {},
        "failure_policy": task.failure_policy or {},
        "type_config": task.type_config or {},
        "stats": stats,
        "runtime_stage": derive_task_runtime_stage(task, actions=actions),
        "target_summary": target_summary,
        "search_text": " ".join(str(item) for item in search_parts if item),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _stats_with_account_coverage(session: Session, task: Task, stats: dict[str, Any]) -> dict[str, Any]:
    result = dict(stats or {})
    coverage = task_account_coverage(session, task)
    if coverage:
        result["account_coverage"] = coverage
    return result


def _target_summary(session: Session, task: Task) -> str:
    config = task.type_config or {}
    if task.type.startswith("channel_"):
        channel = _channel_for_config(session, task)
        if channel:
            return f"{channel.title} @{channel.username}" if channel.username else channel.title
        return str(config.get("target_channel_name") or "")
    if task.type == "group_ai_chat":
        return str(
            _operation_target_title(session, task.tenant_id, config.get("target_operation_target_id"))
            or config.get("target_group_name")
            or config.get("target_group_id")
            or ""
        )
    if task.type == "group_relay":
        sources = [
            str(item.get("group_name") or _operation_target_title(session, task.tenant_id, item.get("operation_target_id")) or item.get("group_id") or "")
            for item in config.get("source_groups") or []
            if isinstance(item, dict)
        ]
        targets = [
            _operation_target_title(session, task.tenant_id, item) or f"运营目标#{item}"
            for item in config.get("target_operation_target_ids") or []
        ]
        targets.extend(str(item) for item in config.get("target_group_ids") or [])
        if config.get("target_operation_target_id"):
            label = _operation_target_title(session, task.tenant_id, config.get("target_operation_target_id")) or f"运营目标#{config.get('target_operation_target_id')}"
            if label not in targets:
                targets.insert(0, label)
        if config.get("target_group_id") and str(config.get("target_group_id")) not in targets:
            targets.insert(0, str(config.get("target_group_id")))
        return " ".join([*sources, *targets])
    return ""


def _operation_target_title(session: Session, tenant_id: int, target_id: Any) -> str:
    try:
        parsed_id = int(target_id or 0)
    except (TypeError, ValueError):
        return ""
    if not parsed_id:
        return ""
    target = session.get(OperationTarget, parsed_id)
    if not target or target.tenant_id != tenant_id:
        return ""
    return target.title


def _task_config_search_text(session: Session, task: Task) -> str:
    config = task.type_config or {}
    parts: list[str] = []
    if task.type.startswith("channel_"):
        channel = _channel_for_config(session, task)
        if channel:
            parts.extend([channel.title, channel.username, channel.tg_peer_id])
        message_ids = [int(item) for item in config.get("message_ids") or [] if str(item).isdigit()]
        if message_ids:
            stmt = select(ChannelMessage).where(ChannelMessage.tenant_id == task.tenant_id)
            if channel:
                stmt = stmt.where(ChannelMessage.channel_target_id == channel.id)
            stmt = stmt.where((ChannelMessage.id.in_(message_ids)) | (ChannelMessage.message_id.in_(message_ids)))
            for message in session.scalars(stmt):
                parts.extend([str(message.message_id), message.content_preview, message.message_url])
    return " ".join(part for part in parts if part)


def _search_actions(session: Session, task: Task) -> list[Action]:
    return list(
        session.scalars(
            select(Action)
            .where(Action.task_id == task.id)
            .order_by(Action.created_at.desc())
            .limit(20)
        )
    )


def _channel_for_config(session: Session, task: Task) -> OperationTarget | None:
    config = task.type_config or {}
    target_id = int(config.get("target_channel_id") or 0)
    if not target_id:
        return None
    channel = session.get(OperationTarget, target_id)
    if not channel or channel.tenant_id != task.tenant_id or channel.target_type != "channel":
        return None
    return channel


def _detail_accounts(session: Session, actions: list[Action]) -> list[dict[str, Any]]:
    ids = sorted({int(action.account_id) for action in actions if action.account_id})
    if not ids:
        return []
    accounts = list(session.scalars(select(TgAccount).where(TgAccount.id.in_(ids))))
    by_id = {account.id: account for account in accounts}
    return [
        {
            "id": account_id,
            "display_name": by_id[account_id].display_name if account_id in by_id else f"账号 #{account_id}",
            "username": by_id[account_id].username if account_id in by_id else None,
            "status": by_id[account_id].status if account_id in by_id else "未知",
        }
        for account_id in ids
    ]


def _membership_phase(task: Task, actions: list[Action] | None = None) -> dict[str, Any]:
    stats = task.stats or {}
    if isinstance(stats, dict) and _has_membership_stats(stats):
        return _deduped_membership_phase(actions, stats)
    if actions:
        rows = [action for action in actions if action.action_type in {"ensure_channel_membership", "ensure_target_membership"}]
        if rows:
            return _membership_phase_from_actions(rows)
    if not isinstance(stats, dict):
        return {}
    legacy = {
        "stage": stats.get("membership_stage") or "",
        "summary": stats.get("membership_summary") or {},
        "joined_count": int(stats.get("membership_joined_count") or 0),
        "need_join_count": int(stats.get("membership_need_join_count") or 0),
        "failed_count": int(stats.get("membership_failed_count") or 0),
    }
    return {**_membership_phase_from_stats(stats), **legacy}


def _deduped_membership_phase(actions: list[Action] | None, stats: dict[str, Any]) -> dict[str, Any]:
    legacy = {
        "stage": stats.get("membership_stage") or "",
        "summary": stats.get("membership_summary") or {},
        "joined_count": int(stats.get("membership_joined_count") or 0),
        "need_join_count": int(stats.get("membership_need_join_count") or 0),
        "failed_count": int(stats.get("membership_failed_count") or 0),
    }
    phase = {**_membership_phase_from_stats(stats), **legacy}
    rows = [action for action in (actions or []) if action.action_type in {"ensure_channel_membership", "ensure_target_membership"}]
    if rows:
        phase["ready_account_count"] = sum(1 for action in rows if _membership_action_ready_before_task(action))
    return phase


def _has_membership_stats(stats: dict[str, Any]) -> bool:
    return any(
        key in stats
        for key in (
            "membership_summary",
            "membership_joined_count",
            "membership_need_join_count",
            "membership_failed_count",
        )
    )


def _membership_phase_from_actions(rows: list[Action]) -> dict[str, Any]:
    pending_statuses = {"pending", "retryable_failed"}
    success = sum(1 for action in rows if _membership_action_succeeded(action))
    ready = sum(1 for action in rows if _membership_action_ready_before_task(action))
    running = sum(1 for action in rows if action.status in {"claiming", "executing"})
    pending = sum(1 for action in rows if action.status in pending_statuses)
    failed = sum(1 for action in rows if _membership_action_failed(action))
    unknown = sum(1 for action in rows if action.status == "unknown_after_send")
    blocked = sum(1 for action in rows if _membership_action_blocked(action))
    target_count = len({_membership_target_key(action) for action in rows if _membership_target_key(action)})
    estimated_finish_at = max([action.scheduled_at for action in rows if action.status in pending_statuses and action.scheduled_at], default=None)
    status = _membership_status(success, pending, running, failed, len(rows), unknown=unknown)
    return {
        "stage": _membership_stage(status),
        "status": status,
        "progress_percent": _membership_progress(success, failed, len(rows)),
        "current_phase": _membership_current_phase(status, running=running, pending=pending, failed=failed, unknown=unknown),
        "warnings": _membership_warnings(rows),
        "summary": {
            "target_count": target_count,
            "action_count": len(rows),
            "running_account_count": running,
            "success_account_count": success,
            "unknown_after_send_count": unknown,
            "estimated_finish_at": estimated_finish_at,
        },
        "joined_count": success,
        "need_join_count": pending,
        "ready_account_count": ready,
        "pending_account_count": pending,
        "running_account_count": running,
        "success_account_count": success,
        "failed_account_count": failed,
        "unknown_after_send_count": unknown,
        "blocked_account_count": blocked + unknown,
        "failed_count": failed,
        "running_count": running,
        "success_count": success,
        "estimated_finish_at": estimated_finish_at,
    }


def _membership_phase_from_stats(stats: dict[str, Any]) -> dict[str, Any]:
    summary = stats.get("membership_summary") if isinstance(stats.get("membership_summary"), dict) else {}
    success = int(stats.get("membership_joined_count") or summary.get("success_account_count") or 0)
    pending = int(stats.get("membership_need_join_count") or summary.get("pending_account_count") or 0)
    failed = int(stats.get("membership_failed_count") or summary.get("failed_account_count") or 0)
    unknown = int(stats.get("membership_unknown_after_send_count") or summary.get("unknown_after_send_count") or 0)
    running = int(summary.get("running_account_count") or 0)
    total = success + pending + failed + running + unknown
    status = _membership_status(success, pending, running, failed, total, unknown=unknown)
    return {
        "status": status,
        "progress_percent": _membership_progress(success, failed, total),
        "current_phase": _membership_current_phase(status, running=running, pending=pending, failed=failed, unknown=unknown),
        "warnings": list(stats.get("membership_warnings") or []),
        "ready_account_count": int(summary.get("ready_account_count") or success),
        "pending_account_count": pending,
        "running_account_count": running,
        "success_account_count": success,
        "failed_account_count": failed,
        "unknown_after_send_count": unknown,
        "blocked_account_count": int(summary.get("blocked_account_count") or 0) + unknown,
        "schedule_window_hours": int(stats.get("membership_schedule_window_hours") or summary.get("schedule_window_hours") or 0),
        "estimated_finish_at": summary.get("estimated_finish_at"),
    }


def _membership_action_ready_before_task(action: Action) -> bool:
    return _membership_action_succeeded(action)


def _membership_action_succeeded(action: Action) -> bool:
    result = action.result or {}
    if result.get("membership_status") in {"joined", "already_joined"}:
        return True
    if result.get("error_code") == "already_joined":
        return True
    return action.status == "success" and bool(result.get("success"))


def _membership_action_failed(action: Action) -> bool:
    if action.status == "failed" or _membership_action_blocked(action):
        return True
    return action.status == "skipped" and not _membership_action_succeeded(action)


def _membership_action_blocked(action: Action) -> bool:
    result = action.result or {}
    blocked_codes = {"account_unavailable", "manual_required", "permission_denied", "membership_permission_denied"}
    error_code = str(result.get("error_code") or "").lower()
    membership_status = str(result.get("membership_status") or "").lower()
    return error_code in blocked_codes or membership_status in {"permission_denied", "manual_required"}


def _membership_target_key(action: Action) -> str:
    payload = action.payload or {}
    return str(payload.get("channel_target_id") or payload.get("target_operation_target_id") or payload.get("target_peer_id") or "")


def _membership_status(success: int, pending: int, running: int, failed: int, total: int, *, unknown: int = 0) -> str:
    if total <= 0:
        return "not_required"
    if running:
        return "running"
    if pending and success:
        return "partial_success"
    if pending:
        return "pending"
    if unknown and success:
        return "partial_success"
    if unknown:
        return "blocked"
    if failed and success:
        return "partial_success"
    if failed:
        return "failed"
    return "completed"


def _membership_stage(status: str) -> str:
    mapping = {
        "running": "membership_running",
        "pending": "membership_running",
        "partial_success": "membership_partial",
        "failed": "membership_blocked",
        "blocked": "membership_blocked",
        "completed": "membership_ready",
    }
    return mapping.get(status, "membership_ready" if status == "not_required" else status)


def _membership_progress(success: int, failed: int, total: int) -> int:
    return round((success + failed) * 100 / total) if total > 0 else 100


def _membership_current_phase(status: str, *, running: int, pending: int, failed: int, unknown: int = 0) -> str:
    if running:
        return "加入 / 关注中"
    if pending:
        return "排队中"
    if unknown:
        return "等待人工确认"
    if failed and status in {"failed", "blocked"}:
        return "等待人工处理"
    if status == "partial_success":
        return "部分完成"
    if status == "completed":
        return "已完成"
    return "无需准入"


def _membership_warnings(rows: list[Action]) -> list[str]:
    warnings: list[str] = []
    for action in rows:
        result = action.result or {}
        text = str(result.get("warning") or result.get("error_message") or "")
        if action.status == "failed" and text and text not in warnings:
            warnings.append(text)
    return warnings


def _membership_accounts(session: Session, actions: list[Action]) -> list[dict[str, Any]]:
    rows = [action for action in actions if action.action_type in {"ensure_channel_membership", "ensure_target_membership"} and action.account_id]
    account_ids = sorted({int(action.account_id) for action in rows if action.account_id})
    accounts = list(session.scalars(select(TgAccount).where(TgAccount.id.in_(account_ids)))) if account_ids else []
    by_id = {account.id: account for account in accounts}
    latest_attempts = _latest_attempts_by_action(session, rows)
    result: list[dict[str, Any]] = []
    for action in sorted(rows, key=lambda item: (int(item.account_id or 0), item.created_at)):
        payload = action.payload or {}
        action_result = action.result or {}
        account_id = int(action.account_id or 0)
        latest_attempt = latest_attempts.get(action.id)
        result.append(
            {
                "account_id": account_id,
                "display_name": by_id[account_id].display_name if account_id in by_id else f"账号 #{account_id}",
                "username": by_id[account_id].username if account_id in by_id else "",
                "status": action.status,
                "membership_status": action_result.get("membership_status") or action_result.get("error_code") or action.status,
                "failure_reason": action_result.get("error_message") or action_result.get("detail") or "",
                "retry_count": action.retry_count,
                "scheduled_at": action.scheduled_at,
                "executed_at": action.executed_at,
                "completed_at": latest_attempt.after_call_at if latest_attempt and latest_attempt.after_call_at else action.executed_at,
                "channel_target_id": payload.get("channel_target_id"),
                "target_type": payload.get("target_type") or "channel",
                "target_display": payload.get("target_display") or "",
            }
        )
    return result


def _membership_items(session: Session, task: Task, actions: list[Action]) -> list[dict[str, Any]]:
    rows = [action for action in actions if _is_membership_action(action)]
    if not rows:
        return []
    accounts = _accounts_by_id(session, rows)
    groups = _groups_by_target_id(session, task.tenant_id, rows)
    verifications = _verification_tasks_by_group_account(session, task.tenant_id, groups, rows)
    attempts = _latest_attempts_by_action(session, rows)
    return [
        _membership_item_payload(action, accounts, groups, verifications, attempts)
        for action in sorted(rows, key=lambda item: (item.scheduled_at or item.created_at, item.created_at), reverse=True)
    ]


def _is_membership_action(action: Action) -> bool:
    return action.action_type in {"ensure_channel_membership", "ensure_target_membership"} and bool(action.account_id)


def _accounts_by_id(session: Session, actions: list[Action]) -> dict[int, TgAccount]:
    account_ids = sorted({int(action.account_id) for action in actions if action.account_id})
    if not account_ids:
        return {}
    return {account.id: account for account in session.scalars(select(TgAccount).where(TgAccount.id.in_(account_ids)))}


def _groups_by_target_id(session: Session, tenant_id: int, actions: list[Action]) -> dict[int, TgGroup]:
    target_ids = _membership_target_ids(actions)
    targets = list(session.scalars(select(OperationTarget).where(OperationTarget.tenant_id == tenant_id, OperationTarget.id.in_(target_ids)))) if target_ids else []
    peers = [target.tg_peer_id for target in targets if target.target_type == "group" and target.tg_peer_id]
    groups = list(session.scalars(select(TgGroup).where(TgGroup.tenant_id == tenant_id, TgGroup.tg_peer_id.in_(peers)))) if peers else []
    by_peer = {group.tg_peer_id: group for group in groups}
    return {target.id: by_peer[target.tg_peer_id] for target in targets if target.tg_peer_id in by_peer}


def _membership_target_ids(actions: list[Action]) -> list[int]:
    target_ids: set[int] = set()
    for action in actions:
        target_id = _membership_target_id(action.payload or {})
        if target_id:
            target_ids.add(target_id)
    return sorted(target_ids)


def _membership_target_id(payload: dict[str, Any]) -> int | None:
    try:
        return int(payload.get("channel_target_id") or 0) or None
    except (TypeError, ValueError):
        return None


def _verification_tasks_by_group_account(session: Session, tenant_id: int, groups: dict[int, TgGroup], actions: list[Action]) -> dict[tuple[int, int], VerificationTask]:
    group_ids = sorted({group.id for group in groups.values()})
    account_ids = sorted({int(action.account_id) for action in actions if action.account_id})
    if not group_ids or not account_ids:
        return {}
    tasks = list(
        session.scalars(
            select(VerificationTask)
            .where(VerificationTask.tenant_id == tenant_id, VerificationTask.group_id.in_(group_ids), VerificationTask.account_id.in_(account_ids))
            .order_by(VerificationTask.id.desc())
        )
    )
    latest: dict[tuple[int, int], VerificationTask] = {}
    for task in tasks:
        if task.group_id and task.account_id:
            latest.setdefault((task.group_id, task.account_id), task)
    return latest


def _membership_item_payload(
    action: Action,
    accounts: dict[int, TgAccount],
    groups: dict[int, TgGroup],
    verifications: dict[tuple[int, int], VerificationTask],
    attempts: dict[str, ExecutionAttempt],
) -> dict[str, Any]:
    payload = action.payload or {}
    result = action.result or {}
    account_id = int(action.account_id or 0)
    target_id = _membership_target_id(payload)
    group = groups.get(target_id or 0)
    verification = verifications.get((group.id, account_id)) if group else None
    phase = _membership_item_phase(action, verification)
    latest_attempt = attempts.get(action.id)
    account = accounts.get(account_id)
    failure_type = result.get("error_code") or _attempt_failure_type(latest_attempt)
    failure_detail = result.get("error_message") or result.get("detail") or _attempt_failure_detail(latest_attempt)
    recovery = classify_membership_recovery(
        phase=phase,
        account_status=account.status if account else "",
        action_status=action.status,
        failure_type=failure_type,
        failure_detail=failure_detail,
        verification_action=verification.suggested_action if verification else "",
        verification_status=verification.status if verification else "",
        can_auto_resolve=bool(verification.can_auto_resolve) if verification else False,
    )
    return {
        "item_id": action.id,
        "latest_action_id": action.id,
        "account_id": account_id,
        "display_name": account.display_name if account else f"账号 #{account_id}",
        "username": account.username if account else "",
        "status": action.status,
        "phase": phase,
        "can_send": phase == "ready",
        "target_id": target_id,
        "target_type": payload.get("target_type") or "channel",
        "target_display": payload.get("target_display") or "",
        "scheduled_at": action.scheduled_at,
        "completed_at": latest_attempt.after_call_at if latest_attempt and latest_attempt.after_call_at else action.executed_at,
        "failure_type": failure_type,
        "failure_detail": failure_detail,
        "manual_required": phase == "manual_required",
        "verification_task_id": verification.id if verification else None,
        "verification_status": verification.status if verification else "",
        "verification_action": verification.suggested_action if verification else "",
        "can_auto_resolve": bool(verification.can_auto_resolve) if verification else False,
        "challenge_question": verification.detected_reason if verification else "",
        **recovery.as_payload(),
    }


def _membership_item_phase(action: Action, verification: VerificationTask | None) -> str:
    if _membership_item_ready(action):
        return "ready"
    if verification and "没有读取到最近验证聊天信息" in (verification.failure_detail or ""):
        return "challenge_context_empty"
    if verification and not verification.can_auto_resolve:
        return "manual_required"
    if verification and verification.can_auto_resolve:
        if verification.suggested_action == "识别图形验证码" and action.status in {"claiming", "executing"}:
            return "captcha_solving"
        return "challenge_solving" if action.status in {"claiming", "executing"} else "challenge_required"
    if action.status in {"claiming", "executing"}:
        return "joining"
    if action.status in {"pending", "retryable_failed"}:
        return "not_joined"
    if action.status == "unknown_after_send":
        return "manual_required"
    if action.status == "failed":
        return "failed"
    return "manual_required" if (action.result or {}).get("membership_status") == "permission_denied" else action.status


def _membership_item_ready(action: Action) -> bool:
    result = action.result or {}
    if result.get("membership_status") in {"joined", "already_joined"}:
        return True
    return action.status == "success" and bool(result.get("success"))


def _attempt_failure_type(attempt: ExecutionAttempt | None) -> str:
    return attempt.failure_type if attempt and attempt.failure_type else ""


def _attempt_failure_detail(attempt: ExecutionAttempt | None) -> str:
    return attempt.failure_detail if attempt and attempt.failure_detail else ""


def _latest_attempts_by_action(session: Session, actions: list[Action]) -> dict[str, ExecutionAttempt]:
    action_ids = [action.id for action in actions if action.id]
    if not action_ids:
        return {}
    attempts = list(
        session.scalars(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.action_id.in_(action_ids))
            .order_by(ExecutionAttempt.action_id.asc(), ExecutionAttempt.attempt_no.desc())
        )
    )
    latest: dict[str, ExecutionAttempt] = {}
    for attempt in attempts:
        if attempt.action_id not in latest:
            latest[attempt.action_id] = attempt
    return latest


def _message_groups(session: Session, task: Task, actions: list[Action]) -> list[dict[str, Any]]:
    groups: dict[tuple[int | None, int | None, str], dict[str, Any]] = {}
    message_ids = {
        int(action.payload["channel_message_id"])
        for action in actions
        if isinstance(action.payload, dict) and isinstance(action.payload.get("channel_message_id"), int)
    }
    messages = list(session.scalars(select(ChannelMessage).where(ChannelMessage.id.in_(message_ids)))) if message_ids else []
    messages_by_id = {message.id: message for message in messages}
    channel_ids = {
        int(message.channel_target_id)
        for message in messages
        if message.channel_target_id
    } | {
        int(action.payload["channel_target_id"])
        for action in actions
        if isinstance(action.payload, dict) and isinstance(action.payload.get("channel_target_id"), int)
    }
    channels = list(session.scalars(select(OperationTarget).where(OperationTarget.id.in_(channel_ids)))) if channel_ids else []
    channels_by_id = {channel.id: channel for channel in channels}

    for action in actions:
        payload = action.payload or {}
        if not isinstance(payload, dict) or "message_id" not in payload or "channel_id" not in payload:
            continue
        message = messages_by_id.get(payload.get("channel_message_id"))
        channel_target_id = int(payload.get("channel_target_id") or (message.channel_target_id if message else 0) or 0) or None
        channel = channels_by_id.get(channel_target_id) if channel_target_id else None
        action_type = action.action_type
        key = (channel_target_id, int(payload.get("message_id") or 0) or None, action_type)
        target_count = _channel_subtask_configured_target_count(task, action_type)
        item = groups.setdefault(
            key,
            {
                "channel_target_id": channel_target_id,
                "channel_title": channel.title if channel else str(payload.get("target_display") or ""),
                "channel_username": channel.username if channel else "",
                "message_id": key[1],
                "action_type": action_type,
                "action_label": _action_label(action_type),
                "message_url": message.message_url if message else "",
                "content_preview": message.content_preview if message else str(payload.get("message_content") or ""),
                "target_count": target_count,
                "completed_count": 0,
                "failed_count": 0,
                "running_count": 0,
                "skipped_count": 0,
                "duplicate_count": 0,
                "capacity_shortfall": 0,
                "subtask_status": "运行中",
                "stats": {"target": target_count, "total": 0, "pending": 0, "executing": 0, "success": 0, "failed": 0, "skipped": 0, "duplicate": 0, "direct": 0, "reply": 0},
                "actions": [],
            },
        )
        item["actions"].append(action)
        stats = item["stats"]
        stats["total"] += 1
        if payload.get("reply_to_message_id"):
            stats["reply"] += 1
        else:
            stats["direct"] += 1
        if action.status in stats:
            stats[action.status] += 1
        if _is_duplicate_action(action):
            stats["duplicate"] += 1
        if action.result and action.result.get("error_message"):
            stats["last_error"] = action.result["error_message"]
    for item in groups.values():
        stats = item["stats"]
        item["completed_count"] = int(stats.get("success") or 0)
        item["failed_count"] = int(stats.get("failed") or 0)
        item["running_count"] = int(stats.get("pending") or 0) + int(stats.get("executing") or 0)
        item["skipped_count"] = int(stats.get("skipped") or 0)
        item["duplicate_count"] = int(stats.get("duplicate") or 0)
        total_actions = int(stats.get("total") or 0)
        item["target_count"] = _channel_subtask_effective_target_count(task, str(item.get("action_type") or ""), total_actions)
        item["stats"]["target"] = item["target_count"]
        item["capacity_shortfall"] = max(int(item.get("target_count") or 0) - total_actions, 0)
        item["subtask_status"] = _channel_subtask_status(item)
    return sorted(groups.values(), key=lambda item: (item.get("channel_title") or "", -(item.get("message_id") or 0)))


def _channel_subtask_configured_target_count(task: Task, action_type: str) -> int:
    config = task.type_config or {}
    if action_type == "view_message":
        return int(config.get("per_message_daily_view_target") or config.get("target_views_per_message") or 0)
    if action_type == "like_message":
        return int(config.get("target_likes_per_message") or 0)
    if action_type == "post_comment":
        return int(config.get("target_comments_per_message") or 0)
    return 0


def _channel_subtask_effective_target_count(task: Task, action_type: str, planned_count: int) -> int:
    configured_count = _channel_subtask_configured_target_count(task, action_type)
    lower, upper = quantity_jitter_bounds(configured_count, _channel_subtask_jitter_ratio(task, action_type))
    if planned_count and lower <= planned_count <= upper:
        return planned_count
    if planned_count < lower:
        return lower
    return configured_count


def _channel_subtask_jitter_ratio(task: Task, action_type: str) -> float:
    config = task.type_config or {}
    if action_type == "view_message":
        return float(config.get("view_count_jitter") or 0)
    if action_type == "like_message":
        return float(config.get("like_count_jitter") or 0)
    if action_type == "post_comment":
        return float(config.get("comment_count_jitter") or 0)
    return 0.0


def _action_label(action_type: str) -> str:
    return {
        "view_message": "浏览",
        "like_message": "点赞",
        "post_comment": "评论/回复",
        "send_message": "发送",
    }.get(action_type, action_type)


def _is_duplicate_action(action: Action) -> bool:
    result = action.result or {}
    text = " ".join(str(result.get(key) or "") for key in ("error_code", "failure_type", "error_message", "detail")).lower()
    return "duplicate" in text or "重复" in text


def _channel_subtask_status(item: dict[str, Any]) -> str:
    if item.get("capacity_shortfall"):
        return "容量不足"
    if item.get("running_count"):
        return "运行中"
    if item.get("target_count") and item.get("completed_count", 0) >= item["target_count"]:
        return "已达标"
    if item.get("failed_count"):
        return "有失败"
    if item.get("skipped_count"):
        return "已跳过"
    return "待规划"


def _ai_cycles(actions: list[Action]) -> list[dict[str, Any]]:
    cycles: dict[str, dict[str, Any]] = {}
    for action in actions:
        payload = action.payload or {}
        if not isinstance(payload, dict):
            continue
        cycle_id = str(payload.get("cycle_id") or "")
        if not cycle_id:
            continue
        item = cycles.setdefault(
            cycle_id,
            {
                "cycle_id": cycle_id,
                "context_message_ids": payload.get("context_message_ids") if isinstance(payload.get("context_message_ids"), list) else [],
                "stats": {"total": 0, "pending": 0, "executing": 0, "success": 0, "failed": 0, "skipped": 0},
                "turns": [],
            },
        )
        item["turns"].append(
            {
                "action_id": action.id,
                "turn_index": int(payload.get("turn_index") or len(item["turns"]) + 1),
                "account_id": action.account_id,
                "account_role": str(payload.get("account_role") or ""),
                "account_memory": str(payload.get("account_memory") or ""),
                "account_profile": str(payload.get("account_profile") or ""),
                "topic_thread": str(payload.get("topic_thread") or ""),
                "topic_plan": str(payload.get("topic_plan") or ""),
                "intent": str(payload.get("intent") or ""),
                "content": str(payload.get("message_text") or ""),
                "reply_to_message_id": int(payload.get("reply_to_message_id")) if payload.get("reply_to_message_id") else None,
                "reply_target_label": str(payload.get("reply_target_label") or ""),
                "reply_target_author": str(payload.get("reply_target_author") or ""),
                "reply_target_preview": str(payload.get("reply_target_preview") or ""),
                "reply_target_source": str(payload.get("reply_target_source") or ""),
                "status": action.status,
                "scheduled_at": action.scheduled_at,
                "executed_at": action.executed_at,
                "result": action.result or {},
            }
        )
        _group_stats_inc(item["stats"], action.status)
    for item in cycles.values():
        item["turns"].sort(key=lambda row: (row["turn_index"], row["scheduled_at"]))
    return sorted(cycles.values(), key=lambda item: item["cycle_id"])


def _ai_generation_records(actions: list[Action]) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for action in actions:
        payload = action.payload or {}
        if not isinstance(payload, dict):
            continue
        generation_id = str(payload.get("ai_generation_id") or "")
        if not generation_id:
            continue
        record = records.setdefault(
            generation_id,
            {
                "generation_id": generation_id,
                "cycle_id": str(payload.get("cycle_id") or generation_id),
                "status": str(payload.get("ai_generation_status") or ""),
                "generated_count": int(payload.get("ai_generation_count") or 0),
                "token_count": int(payload.get("ai_generation_tokens") or 0),
                "context_message_count": int(payload.get("ai_generation_context_count") or 0),
                "account_memory_count": int(payload.get("ai_generation_memory_count") or 0),
                "profile_scene": str(payload.get("profile_scene") or ""),
                "profile_version": int(payload.get("profile_version") or 0),
                "profile_hit_summary": str(payload.get("profile_hit_summary") or ""),
                "profile_unavailable_reason": str(payload.get("profile_unavailable_reason") or ""),
                "anchor_message_ids": payload.get("anchor_message_ids") if isinstance(payload.get("anchor_message_ids"), list) else [],
                "quality_risks": _quality_risks(payload),
                "skip_reason": str(payload.get("quality_skip_reason") or ""),
                "scheduled_at": action.scheduled_at,
                "created_at": action.created_at,
            },
        )
        record["generated_count"] = max(record["generated_count"], int(payload.get("ai_generation_count") or 0))
        record["token_count"] = max(record["token_count"], int(payload.get("ai_generation_tokens") or 0))
        record["context_message_count"] = max(record["context_message_count"], int(payload.get("ai_generation_context_count") or 0))
        record["account_memory_count"] = max(record["account_memory_count"], int(payload.get("ai_generation_memory_count") or 0))
        if action.created_at < record["created_at"]:
            record["created_at"] = action.created_at
        if action.scheduled_at < record["scheduled_at"]:
            record["scheduled_at"] = action.scheduled_at
    return sorted(records.values(), key=lambda item: item["created_at"], reverse=True)


def _quality_risks(payload: dict[str, Any]) -> list[str]:
    return [
        value
        for value in (
            str(payload.get("duplicate_risk") or "").strip(),
            str(payload.get("hallucination_risk") or "").strip(),
        )
        if value
    ]


def _ai_account_profiles(session: Session, task: Task, actions: list[Action]) -> list[dict[str, Any]]:
    if task.type != "group_ai_chat":
        return []
    account_ids = {int(action.account_id) for action in actions if action.account_id}
    account_config = task.account_config if isinstance(task.account_config, dict) else {}
    for account_id in account_config.get("account_ids") or []:
        try:
            account_ids.add(int(account_id))
        except (TypeError, ValueError):
            continue
    if not account_ids:
        return []
    summaries = account_profile_summaries(session, task, sorted(account_ids), recent_limit=5)
    if not summaries:
        return []
    current_counts = dict(
        session.execute(
            select(Action.account_id, func.count(Action.id))
            .where(
                Action.task_id == task.id,
                Action.task_type == "group_ai_chat",
                Action.action_type == "send_message",
                Action.status == "success",
                Action.account_id.in_(account_ids),
            )
            .group_by(Action.account_id)
        ).all()
    )
    accounts = {account.id: account for account in session.scalars(select(TgAccount).where(TgAccount.id.in_(account_ids)))}
    rows: list[dict[str, Any]] = []
    for account_id in sorted(account_ids):
        summary = summaries.get(str(account_id))
        if not summary:
            continue
        current_count = int(current_counts.get(account_id) or 0)
        total = _profile_total_success(summary)
        account = accounts.get(account_id)
        rows.append(
            {
                "account_id": account_id,
                "display_name": account.display_name if account else f"账号 #{account_id}",
                "username": account.username if account else None,
                "status": account.status if account else "未知",
                "total_success_count": total,
                "current_task_success_count": current_count,
                "cross_task_success_count": max(0, total - current_count),
                "profile_summary": summary,
            }
        )
    return sorted(rows, key=lambda item: (-item["total_success_count"], item["account_id"]))


def _profile_total_success(summary: str) -> int:
    match = re.search(r"历史成功发言\s+(\d+)\s+次", summary or "")
    return int(match.group(1)) if match else 0


def _relay_batches(actions: list[Action]) -> list[dict[str, Any]]:
    batches: dict[str, dict[str, Any]] = {}
    for action in actions:
        payload = action.payload or {}
        if not isinstance(payload, dict):
            continue
        batch_id = str(payload.get("relay_batch_id") or "")
        if not batch_id:
            continue
        item = batches.setdefault(
            batch_id,
            {
                "relay_batch_id": batch_id,
                "stats": {"total": 0, "pending": 0, "executing": 0, "success": 0, "failed": 0, "skipped": 0},
                "source_event_count": 0,
                "material_count": 0,
                "rule_version_count": 0,
                "items": [],
            },
        )
        relay_event_id = str(payload.get("relay_event_id") or "")
        source_group_id = payload.get("source_group_id") if isinstance(payload.get("source_group_id"), int) else None
        source_operation_target_id = payload.get("source_operation_target_id") if isinstance(payload.get("source_operation_target_id"), int) else None
        original_text = str(payload.get("original_text") or "")
        transformed_text = str(payload.get("message_text") or "")
        material_text = original_text or transformed_text
        source_info = str(payload.get("source_info") or "")
        source_group_title = str(payload.get("source_group_title") or "")
        source_sender_name = str(payload.get("source_sender_name") or "")
        if (not source_group_title or not source_sender_name) and " / " in source_info:
            source_group_title = source_group_title or source_info.split(" / ", 1)[0].strip()
            source_sender_name = source_sender_name or source_info.split(" / ", 1)[1].strip()
        source_event_key = f"{source_operation_target_id or source_group_id or '-'}:{relay_event_id or '-'}"
        item["items"].append(
            {
                "action_id": action.id,
                "relay_event_id": relay_event_id,
                "source_event_key": source_event_key,
                "source_group_id": source_group_id,
                "source_operation_target_id": source_operation_target_id,
                "operation_target_id": payload.get("operation_target_id") if isinstance(payload.get("operation_target_id"), int) else None,
                "source_info": source_info,
                "source_group_title": source_group_title,
                "source_sender_name": source_sender_name,
                "source_sender_peer_id": str(payload.get("source_sender_peer_id") or ""),
                "source_sender_username": str(payload.get("source_sender_username") or ""),
                "source_sender_role": str(payload.get("source_sender_role") or ""),
                "source_is_bot": bool(payload.get("source_is_bot") or False),
                "source_filter_reason": str(payload.get("source_filter_reason") or ""),
                "source_remote_message_id": str(payload.get("source_remote_message_id") or ""),
                "source_message_type": str(payload.get("source_message_type") or ""),
                "source_sent_at": payload.get("source_sent_at"),
                "target_display": str(payload.get("target_display") or ""),
                "original_text": original_text,
                "transformed_text": transformed_text,
                "material_fingerprint": content_fingerprint(material_text) if material_text else "",
                "rule_set_id": payload.get("rule_set_id") if isinstance(payload.get("rule_set_id"), int) else None,
                "rule_set_name": str(payload.get("rule_set_name") or ""),
                "rule_set_version_id": payload.get("rule_set_version_id") if isinstance(payload.get("rule_set_version_id"), int) else None,
                "resolved_rule_set_version_id": payload.get("resolved_rule_set_version_id") if isinstance(payload.get("resolved_rule_set_version_id"), int) else payload.get("rule_set_version_id") if isinstance(payload.get("rule_set_version_id"), int) else None,
                "rule_set_version": payload.get("rule_set_version") if isinstance(payload.get("rule_set_version"), int) else None,
                "rule_binding_mode": str(payload.get("rule_binding_mode") or ""),
                "rule_trace": payload.get("rule_trace") if isinstance(payload.get("rule_trace"), dict) else {},
                "account_id": action.account_id,
                "status": action.status,
                "retry_count": int(action.retry_count or 0),
                "scheduled_at": action.scheduled_at,
                "executed_at": action.executed_at,
                "result": action.result or {},
            }
        )
        _group_stats_inc(item["stats"], action.status)
    for item in batches.values():
        item["items"].sort(key=lambda row: row["scheduled_at"])
        item["source_event_count"] = len({row["source_event_key"] for row in item["items"] if row.get("source_event_key") and not str(row.get("source_event_key")).endswith(":-")})
        item["material_count"] = len({row["material_fingerprint"] for row in item["items"] if row.get("material_fingerprint")})
        item["rule_version_count"] = len({row["resolved_rule_set_version_id"] or row["rule_set_version_id"] for row in item["items"] if row.get("resolved_rule_set_version_id") or row.get("rule_set_version_id")})
    return sorted(batches.values(), key=lambda item: item["relay_batch_id"])


def _relay_recent_sources(session: Session, task: Task, limit: int = 30) -> list[dict[str, Any]]:
    if task.type != "group_relay":
        return []
    groups: list[TgGroup] = []
    seen_group_ids: set[int] = set()
    for item in (task.type_config or {}).get("source_groups") or []:
        if not isinstance(item, dict) or not item.get("is_active", True):
            continue
        group = _group_for_relay_source(session, task.tenant_id, item)
        if group and group.id not in seen_group_ids:
            groups.append(group)
            seen_group_ids.add(group.id)
    if not groups:
        return []
    rows = list(
        session.scalars(
            select(GroupContextMessage)
            .where(GroupContextMessage.tenant_id == task.tenant_id, GroupContextMessage.group_id.in_([group.id for group in groups]))
            .order_by(GroupContextMessage.sent_at.desc().nullslast(), GroupContextMessage.created_at.desc())
            .limit(limit)
        )
    )
    titles = {group.id: group.title for group in groups}
    return [
        {
            "source_group_id": item.group_id,
            "source_group_title": titles.get(item.group_id, ""),
            "listener_account_id": item.listener_account_id,
            "sender_peer_id": item.sender_peer_id,
            "sender_name": item.sender_name,
            "sender_username": getattr(item, "sender_username", "") or "",
            "sender_role": getattr(item, "sender_role", "") or "",
            "is_bot": bool(getattr(item, "is_bot", False)),
            "source_filter_reason": relay_source_filter_reason(item, task.type_config or {}),
            "content": item.content,
            "message_type": item.message_type,
            "remote_message_id": item.remote_message_id,
            "sent_at": item.sent_at,
        }
        for item in rows
    ]


def _group_for_relay_source(session: Session, tenant_id: int, source: dict[str, Any]) -> TgGroup | None:
    group_id = source.get("group_id") if isinstance(source.get("group_id"), int) else None
    if group_id:
        group = session.get(TgGroup, group_id)
        if group and group.tenant_id == tenant_id:
            return group
    operation_target_id = source.get("operation_target_id") if isinstance(source.get("operation_target_id"), int) else None
    if not operation_target_id:
        return None
    target = session.get(OperationTarget, operation_target_id)
    if not target or target.tenant_id != tenant_id:
        return None
    return session.scalar(select(TgGroup).where(TgGroup.tenant_id == tenant_id, TgGroup.tg_peer_id == target.tg_peer_id).limit(1))


def _group_stats_inc(stats: dict[str, int], status: str) -> None:
    stats["total"] = int(stats.get("total") or 0) + 1
    if status in stats:
        stats[status] = int(stats.get(status) or 0) + 1
