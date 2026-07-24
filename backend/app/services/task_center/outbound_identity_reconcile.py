"""Controlled reconciliation for outbound rows that predate frozen target identity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ChannelMessage, MessageTask, OperationTarget, OperationTask, OperationTaskAttempt, Task, TgGroup
from app.services._common import audit
from app.services.task_center import hard_hourly
from app.services.task_center.datetime_compat import parse_zone, to_zone
from app.services.task_center.hard_hourly_ledger import ensure_bucket
from app.services.task_center.target_lifecycle import action_has_gateway_started


OPEN_ACTION_STATUSES = ("pending", "claiming", "executing")
OPEN_ACTION_TYPES = (
    "send_message",
    "view_message",
    "like_message",
    "post_comment",
    "ensure_channel_membership",
    "ensure_target_membership",
)
CHANNEL_TARGET_ACTION_TYPES = frozenset(
    {"view_message", "like_message", "post_comment", "ensure_channel_membership", "ensure_target_membership"}
)
OPEN_OPERATION_TASK_TYPES = ("MESSAGE_SEND", "CHANNEL_VIEW", "CHANNEL_REACTION", "CHANNEL_REPLY")


@dataclass(frozen=True)
class OutboundIdentityInventory:
    unresolved_action_count: int = 0
    unresolved_message_task_count: int = 0
    unresolved_operation_attempt_count: int = 0

    @property
    def total(self) -> int:
        return (
            self.unresolved_action_count
            + self.unresolved_message_task_count
            + self.unresolved_operation_attempt_count
        )


@dataclass(frozen=True)
class OutboundIdentityReconcileResult:
    bound_action_count: int
    bound_message_task_count: int
    bound_operation_task_count: int
    inventory: OutboundIdentityInventory


def outbound_identity_inventory(session: Session, tenant_id: int | None = None) -> OutboundIdentityInventory:
    unresolved_actions = sum(1 for action in _open_actions(session, tenant_id) if _action_is_unresolved(session, action))
    unresolved_messages = sum(
        1 for task in _open_message_tasks(session, tenant_id) if not _message_task_has_frozen_identity(session, task)
    )
    unresolved_attempts = sum(
        1
        for task, attempt in _open_operation_attempts(session, tenant_id)
        if attempt.gateway_call_started_at is None and not _operation_task_has_frozen_identity(session, task)
    )
    return OutboundIdentityInventory(unresolved_actions, unresolved_messages, unresolved_attempts)


def reconcile_outbound_identity(
    session: Session,
    *,
    tenant_id: int | None,
    actor: str,
    apply: bool,
) -> OutboundIdentityReconcileResult:
    bound_actions = _reconcile_actions(session, tenant_id, apply)
    bound_messages = _reconcile_message_tasks(session, tenant_id, apply)
    bound_operations = _reconcile_operation_tasks(session, tenant_id, apply)
    inventory = outbound_identity_inventory(session, tenant_id)
    result = OutboundIdentityReconcileResult(bound_actions, bound_messages, bound_operations, inventory)
    if apply:
        _audit_reconcile(session, tenant_id, actor, result)
    return result


def _open_actions(session: Session, tenant_id: int | None) -> list[Action]:
    stmt = select(Action).where(
        Action.action_type.in_(OPEN_ACTION_TYPES),
        Action.status.in_(OPEN_ACTION_STATUSES),
    )
    if tenant_id is not None:
        stmt = stmt.where(Action.tenant_id == tenant_id)
    return list(session.scalars(stmt))


def _open_message_tasks(session: Session, tenant_id: int | None) -> list[MessageTask]:
    stmt = select(MessageTask).where(MessageTask.status.in_(("排队中", "发送中")))
    if tenant_id is not None:
        stmt = stmt.where(MessageTask.tenant_id == tenant_id)
    return [task for task in session.scalars(stmt) if task.gateway_call_started_at is None]


def _open_operation_attempts(session: Session, tenant_id: int | None) -> list[tuple[OperationTask, OperationTaskAttempt]]:
    stmt = (
        select(OperationTask, OperationTaskAttempt)
        .join(OperationTaskAttempt, OperationTaskAttempt.task_id == OperationTask.id)
        .where(
            OperationTask.task_type.in_(OPEN_OPERATION_TASK_TYPES),
            OperationTaskAttempt.status == "排队中",
            OperationTaskAttempt.gateway_call_started_at.is_(None),
        )
    )
    if tenant_id is not None:
        stmt = stmt.where(OperationTask.tenant_id == tenant_id)
    return list(session.execute(stmt))


def _action_is_unresolved(session: Session, action: Action) -> bool:
    return not action_has_gateway_started(session, action) and not _action_has_frozen_identity(session, action)


def _action_has_frozen_identity(session: Session, action: Action) -> bool:
    payload = action.payload if isinstance(action.payload, dict) else {}
    target_id = _action_frozen_target_id(action, payload)
    target = _target_for_id(session, action.tenant_id, _positive_int(target_id))
    return _target_identity_matches(target, target_id, payload.get("target_reference_revision"), payload.get("target_reference_snapshot"))


def _action_frozen_target_id(action: Action, payload: dict) -> object:
    if action.action_type in CHANNEL_TARGET_ACTION_TYPES:
        return payload.get("channel_target_id")
    return payload.get("target_operation_target_id") or payload.get("operation_target_id")


def _action_target(session: Session, action: Action) -> OperationTarget | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    target_id = _action_frozen_target_id(action, payload)
    if _action_has_frozen_identity(session, action):
        return _target_for_id(session, action.tenant_id, _positive_int(target_id))
    if action.action_type in CHANNEL_TARGET_ACTION_TYPES:
        return _target_for_peer(session, action.tenant_id, str(payload.get("channel_id") or ""))
    group_id = _positive_int(payload.get("group_id"))
    group = session.get(TgGroup, group_id) if group_id else None
    peer_id = str(payload.get("chat_id") or (group.tg_peer_id if group else "") or "")
    return _target_for_peer(session, action.tenant_id, peer_id)


def _message_task_target(session: Session, task: MessageTask) -> OperationTarget | None:
    target = session.get(OperationTarget, task.operation_target_id) if task.operation_target_id else None
    if target is not None and target.tenant_id == task.tenant_id:
        return target
    if task.operation_target_id is not None:
        return None
    group = session.get(TgGroup, task.group_id) if task.group_id else None
    peer_id = str(task.target_peer_id or (group.tg_peer_id if group else "") or "")
    return _target_for_peer(session, task.tenant_id, peer_id)


def _message_task_has_frozen_identity(session: Session, task: MessageTask) -> bool:
    target = _message_task_target(session, task)
    return _target_identity_matches(
        target,
        task.operation_target_id,
        task.target_reference_revision,
        task.target_reference_snapshot,
    )


def _operation_task_target(session: Session, task: OperationTask) -> OperationTarget | None:
    target = session.get(OperationTarget, task.target_id) if task.target_id else None
    if target is not None and target.tenant_id == task.tenant_id:
        return target
    if task.target_id is not None:
        return None
    message = session.get(ChannelMessage, task.channel_message_id) if task.channel_message_id else None
    if message is None or message.tenant_id != task.tenant_id:
        return None
    return _target_for_id(session, task.tenant_id, message.channel_target_id)


def _operation_task_has_frozen_identity(session: Session, task: OperationTask) -> bool:
    target = _operation_task_target(session, task)
    return _target_identity_matches(
        target,
        task.target_id,
        task.target_reference_revision,
        task.target_reference_snapshot,
    )


def _target_for_peer(session: Session, tenant_id: int, peer_id: str) -> OperationTarget | None:
    if not peer_id:
        return None
    return session.scalar(
        select(OperationTarget).where(
            OperationTarget.tenant_id == tenant_id,
            OperationTarget.tg_peer_id == peer_id,
        )
    )


def _target_for_id(session: Session, tenant_id: int, target_id: int | None) -> OperationTarget | None:
    if target_id is None:
        return None
    return session.scalar(
        select(OperationTarget).where(
            OperationTarget.tenant_id == tenant_id,
            OperationTarget.id == target_id,
        )
    )


def _reconcile_actions(session: Session, tenant_id: int | None, apply: bool) -> int:
    bound = 0
    for action in _open_actions(session, tenant_id):
        if action_has_gateway_started(session, action):
            continue
        target = _action_target(session, action)
        if target is not None and _apply_action_identity(session, action, target, apply):
            bound += 1
    return bound


def _reconcile_message_tasks(session: Session, tenant_id: int | None, apply: bool) -> int:
    bound = 0
    for task in _open_message_tasks(session, tenant_id):
        target = _message_task_target(session, task)
        if target is not None and _apply_message_identity(task, target, apply):
            bound += 1
    return bound


def _reconcile_operation_tasks(session: Session, tenant_id: int | None, apply: bool) -> int:
    bound = 0
    seen: set[int] = set()
    for task, _attempt in _open_operation_attempts(session, tenant_id):
        if task.id in seen:
            continue
        seen.add(task.id)
        if _operation_task_has_gateway_started_attempt(session, task.id):
            continue
        target = _operation_task_target(session, task)
        if target is not None and _apply_operation_identity(task, target, apply):
            bound += 1
    return bound


def _operation_task_has_gateway_started_attempt(session: Session, task_id: int) -> bool:
    return session.scalar(
        select(OperationTaskAttempt.id)
        .where(
            OperationTaskAttempt.task_id == task_id,
            OperationTaskAttempt.gateway_call_started_at.is_not(None),
        )
        .limit(1)
    ) is not None


def _apply_action_identity(session: Session, action: Action, target: OperationTarget, apply: bool) -> bool:
    payload = dict(action.payload or {})
    changed = _apply_identity_values(payload, target, _action_identity_target_key(action))
    changed = _apply_hard_hourly_anchor(session, action, target, payload, apply) or changed
    if changed and apply:
        action.payload = payload
    return changed


def _apply_hard_hourly_anchor(
    session: Session,
    action: Action,
    target: OperationTarget,
    payload: dict,
    apply: bool,
) -> bool:
    if not payload.get("hard_hourly_target"):
        return False
    task = session.get(Task, action.task_id)
    if task is None:
        raise ValueError(f"hard hourly action {action.id} is missing its task")
    start = _planned_bucket_start(task, action, payload)
    changed = False
    if not payload.get("hard_hourly_bucket"):
        payload["hard_hourly_bucket"] = start.isoformat()
        changed = True
    if not payload.get("hard_hourly_goal_at_plan"):
        payload["hard_hourly_goal_at_plan"] = hard_hourly.goal(task.type_config or {})
        changed = True
    if not payload.get("task_config_revision"):
        payload["task_config_revision"] = int(task.config_revision or 1)
        changed = True
    if apply:
        ensure_bucket(
            session,
            task=task,
            operation_target_id=target.id,
            target_reference_revision=int(payload["target_reference_revision"]),
            bucket_start=start,
            goal=int(payload["hard_hourly_goal_at_plan"]),
            task_config_revision=int(payload["task_config_revision"]),
        )
    return changed


def _planned_bucket_start(task: Task, action: Action, payload: dict) -> datetime:
    raw = str(payload.get("hard_hourly_bucket") or "")
    value = datetime.fromisoformat(raw) if raw else action.scheduled_at
    return to_zone(value, parse_zone(task.timezone)).replace(minute=0, second=0, microsecond=0)


def _apply_message_identity(task: MessageTask, target: OperationTarget, apply: bool) -> bool:
    values = _identity_values(target)
    changed = any(
        [
            _missing_target_id(task.operation_target_id),
            _missing_reference_revision(task.target_reference_revision),
            _missing_reference_snapshot(task.target_reference_snapshot),
        ]
    )
    if changed and apply:
        task.operation_target_id = target.id
        task.target_reference_revision = values["target_reference_revision"]
        task.target_reference_snapshot = values["target_reference_snapshot"]
    return changed


def _apply_operation_identity(task: OperationTask, target: OperationTarget, apply: bool) -> bool:
    values = _identity_values(target)
    changed = any(
        [
            _missing_target_id(task.target_id),
            _missing_reference_revision(task.target_reference_revision),
            _missing_reference_snapshot(task.target_reference_snapshot),
        ]
    )
    if changed and apply:
        task.target_id = target.id
        task.target_reference_revision = values["target_reference_revision"]
        task.target_reference_snapshot = values["target_reference_snapshot"]
    return changed


def _action_identity_target_key(action: Action) -> str:
    return "channel_target_id" if action.action_type in CHANNEL_TARGET_ACTION_TYPES else "target_operation_target_id"


def _apply_identity_values(payload: dict, target: OperationTarget, target_key: str = "target_operation_target_id") -> bool:
    values = _identity_values(target)
    if target_key != "target_operation_target_id":
        values[target_key] = values.pop("target_operation_target_id")
    changed = False
    for key, value in values.items():
        if _identity_value_missing(key, payload.get(key)):
            payload[key] = value
            changed = True
    return changed


def _identity_value_missing(key: str, value: object) -> bool:
    if key in {"target_operation_target_id", "channel_target_id"}:
        return _missing_target_id(value)
    if key == "target_reference_revision":
        return _missing_reference_revision(value)
    return _missing_reference_snapshot(value)


def _missing_target_id(value: object) -> bool:
    try:
        return int(value or 0) <= 0
    except (TypeError, ValueError, OverflowError):
        return True


def _missing_reference_revision(value: object) -> bool:
    try:
        return int(value or 0) <= 0
    except (TypeError, ValueError, OverflowError):
        return True


def _missing_reference_snapshot(value: object) -> bool:
    return not isinstance(value, dict) or not str(value.get("tg_peer_id") or "").strip()


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _target_identity_matches(
    target: OperationTarget | None,
    target_id: object,
    revision: object,
    snapshot: object,
) -> bool:
    if target is None or _missing_target_id(target_id) or _missing_reference_revision(revision):
        return False
    if _missing_reference_snapshot(snapshot):
        return False
    return (
        _positive_int(target_id) == target.id
        and _positive_int(revision) == int(target.reference_revision or 1)
        and str(snapshot["tg_peer_id"]).strip() == str(target.tg_peer_id or "")
    )


def _identity_values(target: OperationTarget) -> dict:
    return {
        "target_operation_target_id": target.id,
        "target_reference_revision": int(target.reference_revision or 1),
        "target_reference_snapshot": {
            "tg_peer_id": str(target.tg_peer_id),
            "username": str(target.username or ""),
            "title": str(target.title),
        },
    }


def _audit_reconcile(
    session: Session,
    tenant_id: int | None,
    actor: str,
    result: OutboundIdentityReconcileResult,
) -> None:
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="出站目标身份对账",
        target_type="outbound_identity",
        target_id=str(tenant_id or "all"),
        detail=(
            f"actions={result.bound_action_count}; messages={result.bound_message_task_count}; "
            f"operations={result.bound_operation_task_count}; unresolved={result.inventory.total}"
        ),
    )


__all__ = [
    "OutboundIdentityInventory",
    "OutboundIdentityReconcileResult",
    "outbound_identity_inventory",
    "reconcile_outbound_identity",
]
