from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, Task
from app.models.group_clone import (
    CloneAccountSlot,
    CloneDeliveryObligation,
    CloneSenderBindingHistory,
    CloneSourceStreamState,
)
from app.models.telegram_updates import TelegramAuthorizationUpdateSubscription
from app.schemas.task_center import GroupCloneConfig, GroupCloneTaskCreate

from .group_clone_precheck import precheck_group_clone, resolve_clone_config
from .group_clone_start_rows import initialize_start_rows
from .group_mutation_authority import (
    check_and_claim_exclusive_authority,
    compute_route_hash,
    release_exclusive_authority,
    verify_gateway_admission,
)

UNSAFE_CLOSE_ACTION_STATES = ("claiming", "executing", "unknown_after_send")
CANCELLABLE_ACTION_STATES = ("pending", "retryable_failed")
CANCELLABLE_OBLIGATION_STATES = (
    "observed", "waiting_source_base", "waiting_binding", "waiting_album",
    "waiting_dependency", "waiting_transport", "waiting_manual_review", "ready",
    "action_bound",
)


def start_existing_group_clone(session: Session, task: Task) -> None:
    if task.status not in {"draft", "prepared", "stopped", "failed"}:
        raise ValueError(f"group_clone 当前状态 {task.status} 不可启动")
    payload = _task_payload(task)
    precheck = precheck_group_clone(session, task.tenant_id, payload)
    if precheck.hard_blocks:
        raise ValueError(f"group_clone start precheck failed: {precheck.hard_blocks}")
    resolved = _resolved_config(session, task, payload)
    route_hash = _route_hash(payload)
    claimed, reason, authority = check_and_claim_exclusive_authority(
        session,
        task.tenant_id,
        target_peer_type=payload.target.peer_type,
        target_peer_id=payload.target.peer_id,
        writer_kind="group_clone",
        writer_id=task.id,
        route_hash=route_hash,
    )
    if not claimed or authority is None:
        raise ValueError(f"目标群独占写权限申请失败: {reason}")
    initialize_start_rows(
        session, task, payload=payload, resolved=resolved, route_hash=route_hash,
    )
    task.status = "pending"
    task.next_run_at = None
    task.stats = {**dict(task.stats or {}), "clone_start_state": "starting"}
    task.last_error = ""


def pause_group_clone(task: Task) -> None:
    if task.status not in {"running", "pending"}:
        raise ValueError(f"group_clone 当前状态 {task.status} 不可暂停")
    task.status = "paused"
    task.next_run_at = None
    task.stats = {**dict(task.stats or {}), "clone_start_state": "paused"}


def resume_group_clone(session: Session, task: Task) -> None:
    stream = _current_stream(session, task)
    if stream is None:
        raise ValueError("group_clone resume stream missing")
    _require_existing_authority(session, task)
    task.status = "running"
    task.next_run_at = None
    next_state = "running" if stream.state == "live" else "runtime_recovering"
    task.stats = {**dict(task.stats or {}), "clone_start_state": next_state}
    task.last_error = ""


def stop_group_clone_runtime(session: Session, task: Task) -> None:
    _assert_close_safe(session, task)
    _cancel_unstarted_work(session, task)
    _expire_epoch_bindings(session, task)
    stream = _current_stream(session, task)
    if stream is not None:
        stream.state = "stopped"
    subscription = _current_subscription(session, task)
    if subscription is not None:
        subscription.state = "stopped"
        subscription.version = int(subscription.version or 1) + 1
    _release_authority(session, task)
    task.task_lifecycle_epoch = int(task.task_lifecycle_epoch or 1) + 1
    task.status = "stopped"
    task.next_run_at = None
    task.stats = {**dict(task.stats or {}), "clone_start_state": "stopped"}


def reset_group_clone_runtime(session: Session, task: Task) -> None:
    if task.status != "stopped":
        stop_group_clone_runtime(session, task)
    start_existing_group_clone(session, task)


def assert_group_clone_delete_safe(session: Session, task: Task) -> None:
    _assert_close_safe(session, task)
    _cancel_unstarted_work(session, task)
    _expire_epoch_bindings(session, task)
    stream = _current_stream(session, task)
    if stream is not None:
        stream.state = "stopped"
    subscription = _current_subscription(session, task)
    if subscription is not None:
        subscription.state = "stopped"
        subscription.version = int(subscription.version or 1) + 1
    _release_authority(session, task)


def _task_payload(task: Task) -> GroupCloneTaskCreate:
    config = GroupCloneConfig.model_validate(task.type_config or {})
    return GroupCloneTaskCreate(
        name=task.name,
        priority=task.priority,
        timezone=task.timezone,
        scheduled_start=task.scheduled_start,
        scheduled_end=task.scheduled_end,
        max_duration_hours=task.max_duration_hours,
        account_config=task.account_config or {},
        pacing_config=task.pacing_config or {},
        failure_policy=task.failure_policy or {},
        **config.model_dump(mode="python"),
    )


def _resolved_config(session, task, payload):
    blocks: list[str] = []
    resolved = resolve_clone_config(
        session, task.tenant_id, payload=payload, blocks=blocks,
    )
    if resolved is None or blocks:
        raise ValueError(f"group_clone start precheck drift: {blocks}")
    return resolved


def _route_hash(payload) -> str:
    return compute_route_hash(
        payload.source.peer_type,
        payload.source.peer_id,
        target_peer_type=payload.target.peer_type,
        target_peer_id=payload.target.peer_id,
    )


def _current_stream(session, task):
    return session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == task.id,
        CloneSourceStreamState.task_lifecycle_epoch == task.task_lifecycle_epoch,
    ).with_for_update())


def _current_subscription(session, task):
    return session.scalar(select(TelegramAuthorizationUpdateSubscription).where(
        TelegramAuthorizationUpdateSubscription.task_id == task.id,
        TelegramAuthorizationUpdateSubscription.task_epoch == task.task_lifecycle_epoch,
    ).with_for_update())


def _require_existing_authority(session, task) -> None:
    config = GroupCloneConfig.model_validate(task.type_config or {})
    allowed, reason = verify_gateway_admission(
        session,
        task.tenant_id,
        target_peer_type=config.target.peer_type,
        target_peer_id=config.target.peer_id,
        writer_kind="group_clone",
        writer_id=task.id,
    )
    if not allowed:
        raise ValueError(f"group_clone resume authority failed: {reason}")


def _assert_close_safe(session, task) -> None:
    unsafe = session.scalar(select(Action.id).where(
        Action.task_id == task.id,
        Action.status.in_(UNSAFE_CLOSE_ACTION_STATES),
    ).limit(1))
    started = session.scalar(select(ExecutionAttempt.id).join(
        Action, Action.id == ExecutionAttempt.action_id,
    ).where(
        Action.task_id == task.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
        Action.status.not_in(("success", "failed", "skipped", "cancelled")),
    ).limit(1))
    if unsafe or started:
        raise ValueError("group_clone close blocked by executing or unknown mutation")


def _release_authority(session, task) -> None:
    config = GroupCloneConfig.model_validate(task.type_config or {})
    release_exclusive_authority(
        session,
        task.tenant_id,
        target_peer_type=config.target.peer_type,
        target_peer_id=config.target.peer_id,
        writer_kind="group_clone",
        writer_id=task.id,
    )


def _cancel_unstarted_work(session, task) -> None:
    now_value = datetime.now(timezone.utc)
    actions = session.scalars(select(Action).join(
        CloneDeliveryObligation,
        CloneDeliveryObligation.id == Action.obligation_id,
    ).where(
        Action.task_id == task.id,
        CloneDeliveryObligation.epoch == task.task_lifecycle_epoch,
        Action.status.in_(CANCELLABLE_ACTION_STATES),
    ).with_for_update()).all()
    for action in actions:
        action.status = "cancelled"
        action.executed_at = now_value
        action.result = {
            **dict(action.result or {}),
            "reason": "cancelled_by_group_clone_epoch_close",
        }
    obligations = session.scalars(select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.task_id == task.id,
        CloneDeliveryObligation.epoch == task.task_lifecycle_epoch,
        CloneDeliveryObligation.state.in_(CANCELLABLE_OBLIGATION_STATES),
    ).with_for_update()).all()
    for obligation in obligations:
        obligation.state = "cancelled"
        obligation.error_code = "cancelled_by_group_clone_epoch_close"
        obligation.resolved_at = now_value
        obligation.version = int(obligation.version or 1) + 1


def _expire_epoch_bindings(session, task) -> None:
    now_value = datetime.now(timezone.utc)
    bindings = session.scalars(select(CloneSenderBindingHistory).where(
        CloneSenderBindingHistory.task_id == task.id,
        CloneSenderBindingHistory.task_lifecycle_epoch == task.task_lifecycle_epoch,
        CloneSenderBindingHistory.status.in_(("active", "guarded", "eligible")),
    ).with_for_update()).all()
    for binding in bindings:
        binding.status = "expired"
        binding.valid_to = now_value
        binding.reassignment_reason = "task_epoch_closed"
        slot = session.get(CloneAccountSlot, binding.account_slot_id)
        if slot is not None:
            slot.state = "available"
            slot.owner_id = None
            slot.lease_expires_at = None
            slot.version = int(slot.version or 1) + 1


__all__ = [
    "assert_group_clone_delete_safe",
    "pause_group_clone",
    "reset_group_clone_runtime",
    "resume_group_clone",
    "start_existing_group_clone",
    "stop_group_clone_runtime",
]
