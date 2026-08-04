from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ExecutionAttempt,
    SearchClickFulfillmentObligation,
    Task,
)

from .fulfillment_activation import CURRENT_CONTRACT_VERSION


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
PLANNER_MAINTENANCE_ACTION_STATUSES = frozenset(
    {"pending", "retryable_failed"}
)
RESTORABLE_SEARCH_ACTION_STATUSES = frozenset(
    {*PRE_GATEWAY_ACTION_STATUSES, "failed"}
)


def restore_terminal_search_attempts(session: Session, task: Task) -> int:
    if task.type != "search_click":
        return 0
    actions = list(session.scalars(
        select(Action).where(
            Action.task_id == task.id,
            Action.action_type == "search_join",
            Action.status.in_(RESTORABLE_SEARCH_ACTION_STATUSES),
        )
    ))
    bound = [action for action in actions if _bound_search_payload(action)]
    attempts = _latest_attempts_by_action_id(session, bound)
    restored = 0
    for action in bound:
        attempt = attempts.get(action.id)
        if (
            not _terminal_failed_attempt(attempt)
            or not _failed_action_needs_restore(action, attempt)
        ):
            continue
        _restore_failed_action(action, attempt)
        _restore_search_obligation(session, action, attempt)
        restored += 1
    return restored


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
    return _retire_unbound_legacy_actions(
        session,
        task,
        statuses=PRE_GATEWAY_ACTION_STATUSES,
        skip_locked=False,
    )


def retire_unbound_legacy_actions_for_planner(
    session: Session,
    task: Task,
) -> int:
    return _retire_unbound_legacy_actions(
        session,
        task,
        statuses=PLANNER_MAINTENANCE_ACTION_STATUSES,
        skip_locked=True,
    )


def _retire_unbound_legacy_actions(
    session: Session,
    task: Task,
    *,
    statuses: frozenset[str],
    skip_locked: bool,
) -> int:
    action_type = {
        "group_ai_chat": "send_message",
        "search_click": "search_join",
    }.get(task.type)
    if action_type is None:
        return 0
    statement = select(Action).where(
        Action.task_id == task.id,
        Action.action_type == action_type,
        Action.status.in_(statuses),
    )
    if skip_locked:
        statement = statement.with_for_update(skip_locked=True, of=Action)
    actions = list(session.scalars(
        statement
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
        if (
            task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION
            and action.action_type == "send_message"
        ):
            return False
        return not action.primary_quantity_slot_id
    payload = action.payload if isinstance(action.payload, dict) else {}
    return not str(payload.get("search_click_obligation_id") or "")


def _bound_search_payload(action: Action) -> bool:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return bool(
        payload.get("search_click_obligation_id")
        and payload.get("search_click_assignment_id")
    )


def _terminal_failed_attempt(
    attempt: ExecutionAttempt | None,
) -> bool:
    return bool(
        attempt
        and attempt.gateway_call_started_at is not None
        and attempt.after_call_at is not None
        and attempt.status == "failed"
    )


def _failed_action_needs_restore(
    action: Action,
    attempt: ExecutionAttempt,
) -> bool:
    result = attempt.result_snapshot if isinstance(attempt.result_snapshot, dict) else {}
    failure_type = attempt.failure_type or str(result.get("error_code") or "")
    current_error = str((action.result or {}).get("error_code") or "")
    return bool(
        action.status != "failed"
        or action.executed_at != attempt.after_call_at
        or current_error != failure_type
        or action.claim_owner
        or action.lease_owner
    )


def _restore_failed_action(
    action: Action,
    attempt: ExecutionAttempt,
) -> None:
    result = dict(attempt.result_snapshot or {})
    failure_type = attempt.failure_type or str(result.get("error_code") or "")
    result["success"] = False
    result["error_code"] = failure_type
    if attempt.failure_detail:
        result.setdefault("error_message", attempt.failure_detail)
    action.status = "failed"
    action.executed_at = attempt.after_call_at
    action.result = result
    _clear_claim(action)


def _restore_search_obligation(
    session: Session,
    action: Action,
    attempt: ExecutionAttempt,
) -> None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    obligation = session.get(
        SearchClickFulfillmentObligation,
        str(payload.get("search_click_obligation_id") or ""),
    )
    if obligation is None or obligation.source_action_id != action.id:
        return
    if obligation.status not in {"confirmed", "unknown_after_send"}:
        obligation.status = "open"
        obligation.execution_attempt_id = attempt.id


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
    "restore_terminal_search_attempts",
    "retire_legacy_membership_actions",
    "retire_unbound_legacy_actions",
]
