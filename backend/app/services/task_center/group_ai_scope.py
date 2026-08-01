from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import String, and_, cast, exists, func, select
from sqlalchemy.orm import Session, aliased

from app.models import (
    Action,
    AiGroupMessageMemory,
    ExecutionAttempt,
    GroupContextMessage,
    OperationTarget,
    Task,
    TgGroup,
    TgGroupAccount,
)

from .payloads import SendMessagePayload


CONTENT_SCOPE_CONTRACT_VERSION = "group_content_scope_v1"


@dataclass(frozen=True)
class GroupAiScopeViolation:
    field: str
    detail: str
    reason_code: str = "cross_group_content_scope_mismatch"

    @property
    def code(self) -> str:
        return self.reason_code


def validate_group_ai_content_scope(
    session: Session,
    action: Action,
    *,
    payload: SendMessagePayload,
    account_id: int | None = None,
) -> GroupAiScopeViolation | None:
    if action.task_type != "group_ai_chat" or action.action_type != "send_message":
        return None
    task = session.get(Task, action.task_id) if action.task_id else None
    group = session.get(TgGroup, payload.group_id) if payload.group_id else None
    violation = _identity_violation(
        session,
        action,
        payload=payload,
        task=task,
        group=group,
    )
    if violation:
        return violation
    violation = _contract_violation(action, payload=payload)
    if violation:
        return violation
    violation = _context_violation(session, action, payload=payload)
    if violation:
        return violation
    violation = _memory_violation(session, action, payload=payload)
    if violation:
        return violation
    return _account_link_violation(
        session,
        action,
        payload=payload,
        account_id=account_id,
    )


def _identity_violation(
    session: Session,
    action: Action,
    *,
    payload: SendMessagePayload,
    task: Task | None,
    group: TgGroup | None,
) -> GroupAiScopeViolation | None:
    if task is None or task.tenant_id != action.tenant_id or task.type != "group_ai_chat":
        return GroupAiScopeViolation("task", "Action 与 AI 活群 Task 的租户或类型不一致")
    if group is None or group.tenant_id != action.tenant_id:
        return GroupAiScopeViolation("group", "payload 目标群不存在或不属于 Action 租户")
    if not _task_target_matches_group(session, task, group):
        return GroupAiScopeViolation("target_group_id", "Task 目标群与 payload 目标群不一致")
    if str(payload.chat_id) != str(group.tg_peer_id):
        return GroupAiScopeViolation("chat_id", "payload Telegram peer 与目标群不一致")
    return None


def _task_target_matches_group(session: Session, task: Task, group: TgGroup) -> bool:
    config = task.type_config or {}
    target_group_id = int(config.get("target_group_id") or 0)
    if target_group_id:
        return target_group_id == int(group.id)
    target_id = int(config.get("target_operation_target_id") or 0)
    target = session.get(OperationTarget, target_id) if target_id else None
    return bool(
        target
        and target.tenant_id == task.tenant_id
        and target.target_type == "group"
        and str(target.tg_peer_id) == str(group.tg_peer_id)
    )


def _contract_violation(
    action: Action,
    *,
    payload: SendMessagePayload,
) -> GroupAiScopeViolation | None:
    scope_values = (
        payload.content_scope_contract_version,
        payload.content_scope_tenant_id,
        payload.content_scope_group_id,
        payload.content_scope_task_id,
    )
    if not any(scope_values):
        return GroupAiScopeViolation(
            "scope_contract",
            "历史 Action 缺少群内容 scope 快照，必须按原槽重规划",
            "scope_contract_missing",
        )
    if payload.chat_mode not in {"reply", "idle_warmup", "bootstrap"}:
        return GroupAiScopeViolation("chat_mode", "AI 活群 Action 缺少 Planner 冻结的会话模式")
    expected = (
        payload.content_scope_contract_version == CONTENT_SCOPE_CONTRACT_VERSION
        and payload.content_scope_tenant_id == action.tenant_id
        and payload.content_scope_group_id == payload.group_id
        and payload.content_scope_task_id == str(action.task_id or "")
    )
    if expected:
        return None
    return GroupAiScopeViolation("scope_contract", "Action 缺少或携带不一致的群内容 scope 快照")


def _context_violation(
    session: Session,
    action: Action,
    *,
    payload: SendMessagePayload,
) -> GroupAiScopeViolation | None:
    context_ids = set(payload.context_message_ids + payload.anchor_message_ids)
    if payload.context_snapshot_message_id:
        context_ids.add(payload.context_snapshot_message_id)
    if context_ids and _scoped_context_count(
        session,
        action,
        payload=payload,
        context_ids=context_ids,
    ) != len(context_ids):
        return GroupAiScopeViolation("context_message_ids", "上下文或 snapshot 不属于 Action 目标群")
    if payload.reply_to_message_id and not _reply_target_exists(session, action, payload):
        return GroupAiScopeViolation("reply_to_message_id", "引用目标不属于 Action 目标群")
    return None


def _scoped_context_count(
    session: Session,
    action: Action,
    *,
    payload: SendMessagePayload,
    context_ids: set[int],
) -> int:
    count = session.scalar(
        select(func.count(GroupContextMessage.id)).where(
            GroupContextMessage.id.in_(context_ids),
            GroupContextMessage.tenant_id == action.tenant_id,
            GroupContextMessage.group_id == payload.group_id,
        )
    )
    return int(count or 0)


def _reply_target_exists(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
) -> bool:
    human_target = session.scalar(select(GroupContextMessage.id).where(
        GroupContextMessage.tenant_id == action.tenant_id,
        GroupContextMessage.group_id == payload.group_id,
        GroupContextMessage.remote_message_id == str(payload.reply_to_message_id),
    ))
    if human_target:
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


def successful_own_history_reply_facts(
    session: Session,
    *,
    tenant_id: int,
    task_id: str,
    group_id: int,
    remote_message_id: str = "",
    exclude_action_id: str = "",
    exclude_used_statuses: tuple[str, ...] = (),
    limit: int = 20,
) -> list[tuple[Action, str]]:
    latest_attempt = (
        select(
            ExecutionAttempt.action_id.label("action_id"),
            func.max(ExecutionAttempt.attempt_no).label("attempt_no"),
        )
        .where(
            ExecutionAttempt.status == "success",
            ExecutionAttempt.remote_message_id != "",
        )
        .group_by(ExecutionAttempt.action_id)
        .subquery()
    )
    filters = _own_history_filters(
        tenant_id=tenant_id,
        task_id=task_id,
        group_id=group_id,
        remote_message_id=remote_message_id,
        exclude_action_id=exclude_action_id,
        exclude_used_statuses=exclude_used_statuses,
    )
    rows = session.execute(
        select(Action, ExecutionAttempt.remote_message_id)
        .join(latest_attempt, latest_attempt.c.action_id == Action.id)
        .join(ExecutionAttempt, and_(
            ExecutionAttempt.action_id == latest_attempt.c.action_id,
            ExecutionAttempt.attempt_no == latest_attempt.c.attempt_no,
        ))
        .where(*filters)
        .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
        .limit(max(1, int(limit)))
    )
    return [(action, str(remote_id)) for action, remote_id in rows]


def _own_history_filters(
    *,
    tenant_id: int,
    task_id: str,
    group_id: int,
    remote_message_id: str,
    exclude_action_id: str,
    exclude_used_statuses: tuple[str, ...],
) -> list:
    filters = [
        Action.tenant_id == tenant_id,
        Action.task_id == task_id,
        Action.task_type == "group_ai_chat",
        Action.action_type == "send_message",
        Action.status == "success",
        Action.payload["group_id"].as_integer() == group_id,
        func.trim(func.coalesce(Action.payload["message_text"].as_string(), "")) != "",
    ]
    if remote_message_id:
        filters.append(ExecutionAttempt.remote_message_id == remote_message_id)
    if exclude_action_id:
        filters.append(Action.id != exclude_action_id)
    if exclude_used_statuses:
        filters.append(_unused_reply_target_filter(
            tenant_id=tenant_id,
            group_id=group_id,
            statuses=exclude_used_statuses,
        ))
    return filters


def _unused_reply_target_filter(
    *,
    tenant_id: int,
    group_id: int,
    statuses: tuple[str, ...],
):
    used_action = aliased(Action)
    return ~exists(select(used_action.id).where(
        used_action.tenant_id == tenant_id,
        used_action.task_type == "group_ai_chat",
        used_action.action_type == "send_message",
        used_action.status.in_(statuses),
        used_action.payload["group_id"].as_integer() == group_id,
        cast(used_action.payload["reply_to_message_id"].as_integer(), String)
        == ExecutionAttempt.remote_message_id,
    ))


def _memory_violation(
    session: Session,
    action: Action,
    *,
    payload: SendMessagePayload,
) -> GroupAiScopeViolation | None:
    if not payload.ai_message_memory_id:
        return None
    memory = session.get(AiGroupMessageMemory, payload.ai_message_memory_id)
    matches = bool(
        memory
        and memory.tenant_id == action.tenant_id
        and memory.group_id == payload.group_id
        and memory.task_id == action.task_id
        and memory.action_id == action.id
    )
    if matches:
        return None
    return GroupAiScopeViolation("ai_message_memory_id", "AI message memory 与 Action 目标群不一致")


def _account_link_violation(
    session: Session,
    action: Action,
    *,
    payload: SendMessagePayload,
    account_id: int | None,
) -> GroupAiScopeViolation | None:
    resolved_account_id = int(account_id or action.account_id or 0)
    if not resolved_account_id:
        return GroupAiScopeViolation("account_id", "AI 活群 Action 缺少发送账号")
    link = session.scalar(select(TgGroupAccount.id).where(
        TgGroupAccount.tenant_id == action.tenant_id,
        TgGroupAccount.group_id == payload.group_id,
        TgGroupAccount.account_id == resolved_account_id,
    ))
    if link:
        return None
    return GroupAiScopeViolation("account_group_link", "发送账号不属于 Action 目标群")
