from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ExecutionAttempt,
    OperationTarget,
    Task,
    TaskGroupDailyTarget,
    TgGroup,
)
from app.models.enums import FailureType
from app.services._common import _now


def current_route_action_disposition(
    session: Session,
    task: Task,
    orphan_target: TaskGroupDailyTarget,
    group: TgGroup,
    target: OperationTarget,
) -> dict:
    actions = _current_route_actions(session, task, orphan_target, group, target)
    attempts_by_action = _attempts_by_action(session, [action.id for action in actions])
    safe_terminal_actions = []
    pending_cancellation_actions = []
    for action in actions:
        descriptor = {"id": action.id, "action_version": int(action.action_version or 1)}
        attempts = attempts_by_action.get(action.id, [])
        if _safe_terminal_route_action(action, attempts):
            safe_terminal_actions.append(descriptor)
            continue
        if _cancellable_pending_route_action(action, attempts):
            pending_cancellation_actions.append(descriptor)
            continue
        raise ValueError("current_route_actions_unsafe")
    return {
        "safe_terminal_actions": safe_terminal_actions,
        "pending_cancellation_actions": pending_cancellation_actions,
    }


def cancel_pending_route_actions(
    session: Session,
    disposition: dict,
    approval_ref: str,
) -> list[str]:
    expected = {
        str(item["id"]): int(item["action_version"])
        for item in disposition["pending_cancellation_actions"]
    }
    if not expected:
        return []
    actions = list(session.scalars(select(Action).where(
        Action.id.in_(expected),
    ).with_for_update()))
    attempts_by_action = _attempts_by_action(session, list(expected))
    if {action.id for action in actions} != set(expected):
        raise ValueError("pending_route_action_missing")
    for action in actions:
        _verify_pending_action(action, expected, attempts_by_action)
    for action in actions:
        _cancel_pending_route_action(action, approval_ref)
    return sorted(expected)


def _current_route_actions(
    session: Session,
    task: Task,
    orphan_target: TaskGroupDailyTarget,
    group: TgGroup,
    target: OperationTarget,
) -> list[Action]:
    rows = session.scalars(select(Action).where(
        Action.task_id == task.id,
        Action.created_at >= orphan_target.created_at,
    ).order_by(Action.id))
    return [
        action for action in rows
        if _route_action_matches(action, orphan_target.id, group.id, target.id)
    ]


def _route_action_matches(
    action: Action,
    orphan_target_id: str,
    group_id: int,
    target_id: int,
) -> bool:
    payload = action.payload or {}
    return any((
        str(payload.get("daily_group_target_id") or "") == orphan_target_id,
        str(payload.get("group_id") or "") == str(group_id),
        str(payload.get("target_operation_target_id") or "") == str(target_id),
    ))


def _attempts_by_action(session: Session, action_ids: list[str]) -> dict[str, list[ExecutionAttempt]]:
    if not action_ids:
        return {}
    rows: dict[str, list[ExecutionAttempt]] = defaultdict(list)
    for attempt in session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id.in_(action_ids),
    )):
        rows[attempt.action_id].append(attempt)
    return dict(rows)


def _safe_terminal_route_action(action: Action, attempts: list[ExecutionAttempt]) -> bool:
    if action.status == "skipped":
        return not attempts
    if action.status != "closed_unknown":
        return False
    if str((action.result or {}).get("error_code") or "") != FailureType.PEER_INVALID.value:
        return False
    return bool(attempts) and all(_peer_invalid_before_mutation(attempt) for attempt in attempts)


def _peer_invalid_before_mutation(attempt: ExecutionAttempt) -> bool:
    return bool(
        attempt.gateway_call_started_at
        and attempt.failure_type == FailureType.PEER_INVALID.value
        and not attempt.remote_message_id
    )


def _cancellable_pending_route_action(action: Action, attempts: list[ExecutionAttempt]) -> bool:
    return bool(
        action.status == "pending"
        and not attempts
        and not action.lease_owner
        and action.lease_expires_at is None
        and not action.claim_owner
        and not action.claim_token
        and action.claim_expires_at is None
    )


def _verify_pending_action(
    action: Action,
    expected: dict[str, int],
    attempts_by_action: dict[str, list[ExecutionAttempt]],
) -> None:
    if expected[action.id] != int(action.action_version or 1):
        raise ValueError("pending_route_action_version_drift")
    if not _cancellable_pending_route_action(action, attempts_by_action.get(action.id, [])):
        raise ValueError("pending_route_action_state_drift")


def _cancel_pending_route_action(action: Action, approval_ref: str) -> None:
    action.status = "cancelled"
    action.action_version = int(action.action_version or 1) + 1
    action.executed_at = _now()
    action.lease_owner = ""
    action.lease_expires_at = None
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.result = {
        **(action.result or {}),
        "route_repair_disposition": "cancelled_before_ledger_route_restore",
        "route_repair_approval_ref": approval_ref,
    }
