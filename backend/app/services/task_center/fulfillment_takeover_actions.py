from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, Task


LEGACY_MEMBERSHIP_ACTION_TYPES = frozenset(
    {
        "search_join_membership",
        "ensure_channel_membership",
        "ensure_target_membership",
    }
)
PRE_GATEWAY_ACTION_STATUSES = frozenset(
    {"pending", "claiming", "executing", "retryable_failed"}
)


def retire_legacy_membership_actions(session: Session, task: Task) -> int:
    actions = list(session.scalars(
        select(Action).where(
            Action.task_id == task.id,
            Action.action_type.in_(LEGACY_MEMBERSHIP_ACTION_TYPES),
            Action.status.in_(PRE_GATEWAY_ACTION_STATUSES),
        )
    ))
    started_ids = _gateway_started_action_ids(session, actions)
    retired = [action for action in actions if action.id not in started_ids]
    for action in retired:
        action.status = "skipped"
        action.result = {
            **(action.result or {}),
            "error_code": "legacy_membership_retired_by_click_takeover",
            "error_message": "存量混合搜索已切换为纯搜索点击",
        }
        _clear_claim(action)
    return len(retired)


def retire_unbound_legacy_actions(session: Session, task: Task) -> int:
    action_type = {
        "group_ai_chat": "send_message",
        "search_click": "search_join",
    }.get(task.type)
    if action_type is None:
        return 0
    actions = list(session.scalars(
        select(Action).where(
            Action.task_id == task.id,
            Action.action_type == action_type,
            Action.status.in_(PRE_GATEWAY_ACTION_STATUSES),
        )
    ))
    retired = [action for action in actions if _legacy_action_unbound(task, action)]
    attempts = _latest_attempts_by_action_id(session, retired)
    for action in retired:
        _retire_legacy_action(action, attempts.get(action.id))
    return len(retired)


def _latest_attempts_by_action_id(
    session: Session,
    actions: list[Action],
) -> dict[str, ExecutionAttempt]:
    if not actions:
        return {}
    attempts = session.scalars(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.action_id.in_([action.id for action in actions]))
        .order_by(ExecutionAttempt.action_id, ExecutionAttempt.attempt_no.desc())
    )
    result: dict[str, ExecutionAttempt] = {}
    for attempt in attempts:
        result.setdefault(attempt.action_id, attempt)
    return result


def _gateway_started_action_ids(
    session: Session,
    actions: list[Action],
) -> set[str]:
    if not actions:
        return set()
    return set(session.scalars(
        select(ExecutionAttempt.action_id).where(
            ExecutionAttempt.action_id.in_([action.id for action in actions]),
            ExecutionAttempt.gateway_call_started_at.is_not(None),
        )
    ))


def _legacy_action_unbound(task: Task, action: Action) -> bool:
    if task.type == "group_ai_chat":
        return not action.primary_quantity_slot_id
    payload = action.payload if isinstance(action.payload, dict) else {}
    return not str(payload.get("search_click_obligation_id") or "")


def _retire_legacy_action(
    action: Action,
    attempt: ExecutionAttempt | None,
) -> None:
    if attempt is None or attempt.gateway_call_started_at is None:
        action.status = "skipped"
        code = "legacy_action_retired_by_fulfillment_takeover"
    elif attempt.status == "success":
        action.status = "success"
        code = ""
    elif attempt.status == "failed":
        action.status = "failed"
        code = "legacy_action_retired_after_gateway_failed"
    else:
        action.status = "unknown_after_send"
        code = "legacy_action_retired_after_gateway_unknown"
    result = dict(action.result or {})
    if code:
        result["error_code"] = code
        result["error_message"] = "旧履约 Action 已终结，历史 Attempt 保留且不计入新合同"
    action.result = result
    _clear_claim(action)


def _clear_claim(action: Action) -> None:
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.lease_owner = ""
    action.lease_expires_at = None


__all__ = [
    "retire_legacy_membership_actions",
    "retire_unbound_legacy_actions",
]
