"""Operation target lifecycle: active / target_ref_invalid / group_dissolved."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelMessage,
    MessageTask,
    OperationTarget,
    OperationTask,
    OperationTaskAttempt,
    Task,
    TaskAccountDailyCoverage,
    TaskHardHourlyBucket,
    TaskStatus,
    TgGroup,
    TgGroupAccount,
)
from app.services._common import _now, audit


LifecycleStatus = Literal["active", "target_ref_invalid", "group_dissolved"]

LIFECYCLE_ACTIVE = "active"
LIFECYCLE_TARGET_REF_INVALID = "target_ref_invalid"
LIFECYCLE_GROUP_DISSOLVED = "group_dissolved"

ERROR_TARGET_GROUP_DISSOLVED = "target_group_dissolved"
ERROR_TARGET_REF_INVALID = "target_ref_invalid"
ERROR_TARGET_REFERENCE_SUPERSEDED = "target_reference_superseded"
CHANNEL_MESSAGE_ACTION_TYPES = frozenset(
    {"view_message", "like_message", "post_comment", "ensure_channel_membership", "ensure_target_membership"}
)

MSG_GROUP_DISSOLVED = "群里已被解散，已跳过本目标"
MSG_TARGET_REF_INVALID = "目标引用无效，请更新有效邀请链接或用户名"
USERNAME_NOT_FOUND_PATTERN = re.compile(r'no user has\s+["\']?([^"\'\s]+)["\']?\s+as username', re.IGNORECASE)


@dataclass(frozen=True)
class TargetTerminalBlock:
    code: str
    message: str
    lifecycle_status: str
    reference_revision: int


@dataclass(frozen=True)
class TargetLifecycleImpact:
    unstarted_action_count: int
    unknown_action_count: int
    unstarted_message_task_count: int
    unknown_message_task_count: int
    unstarted_operation_task_count: int
    unknown_operation_task_count: int
    coverage_count: int
    single_target_task_count: int


@dataclass(frozen=True)
class TargetLifecycleResult:
    target: OperationTarget
    skipped_actions: int
    skipped_message_tasks: int
    skipped_operation_tasks: int
    blocked_coverage: int
    paused_tasks: int


def terminal_target_block(target: OperationTarget | None) -> TargetTerminalBlock | None:
    if target is None:
        return None
    status = str(getattr(target, "lifecycle_status", None) or LIFECYCLE_ACTIVE)
    revision = int(getattr(target, "reference_revision", None) or 1)
    if status == LIFECYCLE_GROUP_DISSOLVED:
        return TargetTerminalBlock(
            ERROR_TARGET_GROUP_DISSOLVED,
            MSG_GROUP_DISSOLVED,
            status,
            revision,
        )
    if status == LIFECYCLE_TARGET_REF_INVALID:
        return TargetTerminalBlock(
            ERROR_TARGET_REF_INVALID,
            MSG_TARGET_REF_INVALID,
            status,
            revision,
        )
    return None


def action_has_gateway_started(session: Session, action: Action) -> bool:
    result = action.result if isinstance(action.result, dict) else {}
    if result.get("gateway_call_started_at") or result.get("gateway_started_at"):
        return True
    if action.status == "unknown_after_send":
        return True
    from app.models import ExecutionAttempt

    attempt = session.scalar(
        select(ExecutionAttempt.id).where(
            ExecutionAttempt.action_id == action.id,
            ExecutionAttempt.before_call_at.is_not(None),
        ).limit(1)
    )
    return attempt is not None


def lock_target(session: Session, *, tenant_id: int, target_id: int) -> OperationTarget:
    target = session.scalar(
        select(OperationTarget)
        .where(OperationTarget.tenant_id == tenant_id, OperationTarget.id == target_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if target is None:
        raise ValueError("target not found")
    return target


def resolve_action_target(session: Session, action: Action) -> OperationTarget | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    target_id = _action_payload_target_id(action, payload)
    if target_id is None and action.action_type not in CHANNEL_MESSAGE_ACTION_TYPES:
        task = session.get(Task, action.task_id) if action.task_id else None
        target_id = _task_config_target_id(task)
    if target_id is None:
        return None
    target = session.get(OperationTarget, target_id)
    if target is None or target.tenant_id != action.tenant_id:
        return None
    return target


def preview_target_group_dissolution(
    session: Session,
    *,
    target: OperationTarget,
    revision: int | None = None,
) -> TargetLifecycleImpact:
    rev = int(revision or target.reference_revision or 1)
    return _impact_for_target(session, target=target, revision=rev)


def mark_target_group_dissolved(
    session: Session,
    *,
    target: OperationTarget,
    actor: str,
    reason: str,
    evidence_ref: str,
    expected_version: int,
) -> TargetLifecycleResult:
    target = lock_target(session, tenant_id=target.tenant_id, target_id=target.id)
    if str(target.target_type or "") != "group":
        raise ValueError("only group targets can be marked group_dissolved")
    if int(target.lifecycle_version or 1) != int(expected_version):
        raise LookupError("lifecycle_version_conflict")
    return _apply_lifecycle(
        session,
        target=target,
        actor=actor,
        reason=reason,
        evidence_ref=evidence_ref,
        new_status=LIFECYCLE_GROUP_DISSOLVED,
        skip_code=ERROR_TARGET_GROUP_DISSOLVED,
        skip_message=MSG_GROUP_DISSOLVED,
        coverage_blocker=ERROR_TARGET_GROUP_DISSOLVED,
        pause_message=f"目标群已解散：{target.title}",
    )


def mark_target_ref_invalid(
    session: Session,
    *,
    target: OperationTarget,
    actor: str,
    reason: str,
    evidence_ref: str,
    expected_version: int,
) -> TargetLifecycleResult:
    target = lock_target(session, tenant_id=target.tenant_id, target_id=target.id)
    if int(target.lifecycle_version or 1) != int(expected_version):
        raise LookupError("lifecycle_version_conflict")
    return _apply_lifecycle(
        session,
        target=target,
        actor=actor,
        reason=reason,
        evidence_ref=evidence_ref,
        new_status=LIFECYCLE_TARGET_REF_INVALID,
        skip_code=ERROR_TARGET_REF_INVALID,
        skip_message=MSG_TARGET_REF_INVALID,
        coverage_blocker=ERROR_TARGET_REF_INVALID,
        pause_message=f"目标引用无效：{target.title}",
    )


def auto_mark_target_ref_invalid(
    session: Session,
    *,
    target: OperationTarget,
    reference_revision: int | None,
    account_id: int | None,
    failure_detail: str,
    source_ref: str,
) -> TargetLifecycleResult | None:
    """Terminalize only a proven exact username-missing reference in Phase A enforce modes."""
    if reference_revision is None:
        return None
    target = lock_target(session, tenant_id=target.tenant_id, target_id=target.id)
    if not _exact_username_missing(target, failure_detail):
        return None
    if target.lifecycle_status != LIFECYCLE_ACTIVE or int(target.reference_revision or 1) != int(reference_revision):
        return None
    if _has_other_send_evidence(session, target, reference_revision, account_id):
        return None
    return _apply_lifecycle(
        session,
        target=target,
        actor="target-reference-classifier",
        reason=f"精确 username 不存在：{target.username}",
        evidence_ref=source_ref,
        new_status=LIFECYCLE_TARGET_REF_INVALID,
        skip_code=ERROR_TARGET_REF_INVALID,
        skip_message=MSG_TARGET_REF_INVALID,
        coverage_blocker=ERROR_TARGET_REF_INVALID,
        pause_message=f"目标引用无效：{target.title}",
    )


def reactivate_target(
    session: Session,
    *,
    target: OperationTarget,
    actor: str,
    reason: str,
    evidence_ref: str,
    expected_version: int,
    new_peer_id: str | None = None,
    new_username: str | None = None,
) -> OperationTarget:
    target = lock_target(session, tenant_id=target.tenant_id, target_id=target.id)
    if int(target.lifecycle_version or 1) != int(expected_version):
        raise LookupError("lifecycle_version_conflict")
    peer_id = str(new_peer_id or "").strip()
    username = str(new_username or "").strip().lstrip("@")
    if not peer_id and not username:
        raise ValueError("重新激活必须提交重新核验后的目标引用")
    old_status = str(target.lifecycle_status or LIFECYCLE_ACTIVE)
    old_revision = int(target.reference_revision or 1)
    old_lifecycle_version = int(target.lifecycle_version or 1)
    old_peer_id = str(target.tg_peer_id or "")
    _sync_reactivated_target_reference(session, target, peer_id)
    if peer_id:
        target.tg_peer_id = peer_id
    if username:
        target.username = username
    if not _has_reactivated_send_capability(session, target):
        raise ValueError("重新激活前需至少一个账号通过目标 can_send 检查")
    _mark_epoch_terminal(session, target, old_revision, ERROR_TARGET_REFERENCE_SUPERSEDED)
    skip_unstarted_target_actions(
        session,
        target=target,
        revision=old_revision,
        error_code=ERROR_TARGET_REFERENCE_SUPERSEDED,
        error_message="目标引用已更新，旧动作已跳过",
    )
    _skip_unstarted_message_tasks(
        session,
        target=target,
        revision=old_revision,
        error_code=ERROR_TARGET_REFERENCE_SUPERSEDED,
        error_message="目标引用已更新，旧消息任务已跳过",
        reference_peer=old_peer_id,
    )
    _skip_unstarted_operation_tasks(
        session,
        target=target,
        revision=old_revision,
        error_code=ERROR_TARGET_REFERENCE_SUPERSEDED,
        error_message="目标引用已更新，旧运营任务已跳过",
    )
    target.reference_revision = int(target.reference_revision or 1) + 1
    target.lifecycle_status = LIFECYCLE_ACTIVE
    target.lifecycle_reason = reason
    target.lifecycle_detail = evidence_ref
    target.lifecycle_at = _now()
    target.lifecycle_by = actor
    target.lifecycle_version = int(target.lifecycle_version or 1) + 1
    audit(
        session,
        tenant_id=target.tenant_id,
        actor=actor,
        action="重新激活运营目标",
        target_type="operation_target",
        target_id=str(target.id),
        detail=(
            f"lifecycle={old_status}->{target.lifecycle_status}; "
            f"reference_revision={old_revision}->{target.reference_revision}; "
            f"lifecycle_version={old_lifecycle_version}->{target.lifecycle_version}; "
            f"reason={reason}; evidence={evidence_ref}"
        ),
    )
    session.flush()
    return target


def skip_unstarted_target_actions(
    session: Session,
    *,
    target: OperationTarget,
    revision: int,
    error_code: str,
    error_message: str,
) -> int:
    skipped = 0
    for action in _actions_for_target(session, target=target, revision=revision):
        if action.status not in {"pending", "claiming", "executing"}:
            continue
        if action_has_gateway_started(session, action):
            continue
        action.status = "skipped"
        action.result = {
            **(action.result or {}),
            "success": False,
            "error_code": error_code,
            "error_message": error_message,
        }
        skipped += 1
    return skipped


def _skip_unstarted_message_tasks(
    session: Session,
    *,
    target: OperationTarget,
    revision: int,
    error_code: str,
    error_message: str,
    reference_peer: str | None = None,
) -> int:
    skipped = 0
    for task in _message_tasks_for_target(session, target=target, revision=revision, reference_peer=reference_peer):
        if task.status != TaskStatus.QUEUED.value or task.gateway_call_started_at is not None:
            continue
        task.status = TaskStatus.FAILED.value
        task.failure_type = error_code
        task.failure_detail = error_message
        skipped += 1
    return skipped


def _skip_unstarted_operation_tasks(
    session: Session,
    *,
    target: OperationTarget,
    revision: int,
    error_code: str,
    error_message: str,
) -> int:
    skipped = 0
    for task in _operation_tasks_for_target(session, target=target, revision=revision):
        task_skipped = 0
        started_exists = False
        for attempt in session.scalars(
            select(OperationTaskAttempt).where(
                OperationTaskAttempt.task_id == task.id,
            )
        ):
            if attempt.gateway_call_started_at is not None:
                started_exists = True
                continue
            if attempt.status != TaskStatus.QUEUED.value:
                continue
            attempt.status = TaskStatus.FAILED.value
            attempt.failure_type = error_code
            attempt.failure_detail = error_message
            task_skipped += 1
        if not task_skipped:
            continue
        task.failure_type = error_code
        task.failure_detail = error_message
        if not started_exists:
            task.status = TaskStatus.FAILED.value
            task.executed_at = _now()
        skipped += 1
    return skipped


def _apply_lifecycle(
    session: Session,
    *,
    target: OperationTarget,
    actor: str,
    reason: str,
    evidence_ref: str,
    new_status: str,
    skip_code: str,
    skip_message: str,
    coverage_blocker: str,
    pause_message: str,
) -> TargetLifecycleResult:
    revision = int(target.reference_revision or 1)
    previous_status = str(target.lifecycle_status or LIFECYCLE_ACTIVE)
    previous_version = int(target.lifecycle_version or 1)
    target.lifecycle_status = new_status
    target.lifecycle_reason = reason
    target.lifecycle_detail = evidence_ref
    target.lifecycle_at = _now()
    target.lifecycle_by = actor
    target.lifecycle_version = int(target.lifecycle_version or 1) + 1
    _mark_epoch_terminal(session, target, revision, coverage_blocker)
    skipped = skip_unstarted_target_actions(
        session,
        target=target,
        revision=revision,
        error_code=skip_code,
        error_message=skip_message,
    )
    skipped_message_tasks = _skip_unstarted_message_tasks(
        session,
        target=target,
        revision=revision,
        error_code=skip_code,
        error_message=skip_message,
    )
    skipped_operation_tasks = _skip_unstarted_operation_tasks(
        session,
        target=target,
        revision=revision,
        error_code=skip_code,
        error_message=skip_message,
    )
    blocked_coverage = _block_coverage(session, target=target, blocker=coverage_blocker)
    paused = _pause_single_target_tasks(session, target=target, message=pause_message)
    audit(
        session,
        tenant_id=target.tenant_id,
        actor=actor,
        action=f"目标生命周期={new_status}",
        target_type="operation_target",
        target_id=str(target.id),
        detail=(
            f"lifecycle={previous_status}->{new_status}; reference_revision={revision}; "
            f"lifecycle_version={previous_version}->{target.lifecycle_version}; reason={reason}; "
            f"evidence={evidence_ref}; actions={skipped}; message_tasks={skipped_message_tasks}; "
            f"operation_tasks={skipped_operation_tasks}"
        ),
    )
    session.flush()
    return TargetLifecycleResult(
        target=target,
        skipped_actions=skipped,
        skipped_message_tasks=skipped_message_tasks,
        skipped_operation_tasks=skipped_operation_tasks,
        blocked_coverage=blocked_coverage,
        paused_tasks=paused,
    )


def _impact_for_target(session: Session, *, target: OperationTarget, revision: int) -> TargetLifecycleImpact:
    unstarted = 0
    unknown = 0
    for action in _actions_for_target(session, target=target, revision=revision):
        if action.status == "unknown_after_send" or action_has_gateway_started(session, action):
            unknown += 1
        elif action.status in {"pending", "claiming", "executing"}:
            unstarted += 1
    message_unstarted, message_unknown = _message_task_impact(session, target=target, revision=revision)
    operation_unstarted, operation_unknown = _operation_task_impact(session, target=target, revision=revision)
    coverage = session.scalar(
        select(TaskAccountDailyCoverage.id)
        .join(Task, Task.id == TaskAccountDailyCoverage.task_id)
        .where(
            TaskAccountDailyCoverage.tenant_id == target.tenant_id,
            Task.type == "group_ai_chat",
        )
        .limit(1)
    )
    # Count rows for impact (cheap exact filter via payload is hard; use group peer match via tasks).
    coverage_count = 0
    for row in session.scalars(
        select(TaskAccountDailyCoverage).where(TaskAccountDailyCoverage.tenant_id == target.tenant_id)
    ):
        task = session.get(Task, row.task_id)
        if not task:
            continue
        if _task_config_target_id(task) == target.id:
            coverage_count += 1
    single_target = 0
    for task in session.scalars(
        select(Task).where(Task.tenant_id == target.tenant_id, Task.type == "group_ai_chat", Task.deleted_at.is_(None))
    ):
        config = task.type_config if isinstance(task.type_config, dict) else {}
        if _task_config_target_id(task) == target.id and not _is_multi_target(config):
            single_target += 1
    return TargetLifecycleImpact(
        unstarted_action_count=unstarted,
        unknown_action_count=unknown,
        unstarted_message_task_count=message_unstarted,
        unknown_message_task_count=message_unknown,
        unstarted_operation_task_count=operation_unstarted,
        unknown_operation_task_count=operation_unknown,
        coverage_count=coverage_count,
        single_target_task_count=single_target,
    )


def _actions_for_target(session: Session, *, target: OperationTarget, revision: int) -> list[Action]:
    rows = list(
        session.scalars(
            select(Action).where(
                Action.tenant_id == target.tenant_id,
                Action.status.in_(["pending", "claiming", "executing", "unknown_after_send"]),
            )
        )
    )
    matched: list[Action] = []
    for action in rows:
        payload = action.payload if isinstance(action.payload, dict) else {}
        if _channel_action_matches_target(action, payload, target.id, revision):
            matched.append(action)
            continue
        if action.action_type in CHANNEL_MESSAGE_ACTION_TYPES:
            continue
        target_id = _action_payload_target_id(action, payload)
        if target_id is None:
            task = session.get(Task, action.task_id) if action.task_id else None
            target_id = _task_config_target_id(task)
        if target_id != target.id:
            continue
        action_rev = _positive_int(payload.get("target_reference_revision"))
        if action_rev != revision:
            continue
        matched.append(action)
    return matched


def _action_payload_target_id(action: Action, payload: dict) -> int | None:
    key = "channel_target_id" if action.action_type in CHANNEL_MESSAGE_ACTION_TYPES else "target_operation_target_id"
    return _positive_int(payload.get(key) or payload.get("operation_target_id"))


def _channel_action_matches_target(action: Action, payload: dict, target_id: int, revision: int) -> bool:
    if action.action_type not in CHANNEL_MESSAGE_ACTION_TYPES or _positive_int(payload.get("channel_target_id")) != target_id:
        return False
    action_revision = _positive_int(payload.get("target_reference_revision"))
    return action_revision is None or action_revision == revision


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _task_config_target_id(task: Task | None) -> int | None:
    config = task.type_config if task is not None and isinstance(task.type_config, dict) else {}
    return _positive_int(config.get("target_operation_target_id"))


def _message_tasks_for_target(
    session: Session,
    *,
    target: OperationTarget,
    revision: int,
    reference_peer: str | None = None,
) -> list[MessageTask]:
    rows = list(
        session.scalars(
            select(MessageTask).where(
                MessageTask.tenant_id == target.tenant_id,
                MessageTask.status.in_([TaskStatus.QUEUED.value, TaskStatus.SENDING.value]),
            )
        )
    )
    peer_id = str(reference_peer if reference_peer is not None else target.tg_peer_id)
    return [
        row
        for row in rows
        if _message_task_matches_target(row, target.id, revision, peer_id)
    ]


def _message_task_matches_target(
    task: MessageTask,
    target_id: int,
    revision: int,
    peer_id: str,
) -> bool:
    if task.operation_target_id is not None:
        return (
            int(task.operation_target_id) == target_id
            and int(task.target_reference_revision or 0) == revision
        )
    return task.target_reference_revision is None and str(task.target_peer_id or "") == peer_id


def _operation_tasks_for_target(
    session: Session,
    *,
    target: OperationTarget,
    revision: int,
) -> list[OperationTask]:
    rows = list(
        session.scalars(
            select(OperationTask).where(
                OperationTask.tenant_id == target.tenant_id,
                OperationTask.task_type.in_(("MESSAGE_SEND", "CHANNEL_VIEW", "CHANNEL_REACTION", "CHANNEL_REPLY")),
                OperationTask.status.in_([TaskStatus.QUEUED.value, TaskStatus.RUNNING.value]),
            )
        )
    )
    return [row for row in rows if _operation_task_matches_target(session, row, target.id, revision)]


def _operation_task_matches_target(
    session: Session,
    task: OperationTask,
    target_id: int,
    revision: int,
) -> bool:
    task_target_id = _positive_int(task.target_id)
    if task_target_id is None and task.channel_message_id:
        message = session.get(ChannelMessage, task.channel_message_id)
        if message is not None and message.tenant_id == task.tenant_id:
            task_target_id = _positive_int(message.channel_target_id)
    if task_target_id != target_id:
        return False
    task_revision = _positive_int(task.target_reference_revision)
    return task_revision is None or task_revision == revision


def _message_task_impact(session: Session, *, target: OperationTarget, revision: int) -> tuple[int, int]:
    unstarted = 0
    unknown = 0
    for task in _message_tasks_for_target(session, target=target, revision=revision):
        if task.gateway_call_started_at is not None or task.status == TaskStatus.SENDING.value:
            unknown += 1
        else:
            unstarted += 1
    return unstarted, unknown


def _operation_task_impact(session: Session, *, target: OperationTarget, revision: int) -> tuple[int, int]:
    unstarted = 0
    unknown = 0
    for task in _operation_tasks_for_target(session, target=target, revision=revision):
        attempts = list(
            session.scalars(select(OperationTaskAttempt).where(OperationTaskAttempt.task_id == task.id))
        )
        if any(attempt.gateway_call_started_at is not None for attempt in attempts):
            unknown += 1
        if any(
            attempt.status == TaskStatus.QUEUED.value and attempt.gateway_call_started_at is None
            for attempt in attempts
        ):
            unstarted += 1
    return unstarted, unknown


def _block_coverage(session: Session, *, target: OperationTarget, blocker: str) -> int:
    count = 0
    for row in session.scalars(
        select(TaskAccountDailyCoverage).where(TaskAccountDailyCoverage.tenant_id == target.tenant_id)
    ):
        task = session.get(Task, row.task_id)
        if not task:
            continue
        if _task_config_target_id(task) != target.id:
            continue
        row.state = "blocked"
        row.blocker_code = blocker
        row.blocker_detail = blocker
        row.next_eligible_at = None
        count += 1
    return count


def _pause_single_target_tasks(session: Session, *, target: OperationTarget, message: str) -> int:
    paused = 0
    for task in session.scalars(
        select(Task).where(
            Task.tenant_id == target.tenant_id,
            Task.type == "group_ai_chat",
            Task.deleted_at.is_(None),
            Task.status.in_(["running", "pending", "draft", "paused"]),
        )
    ):
        config = task.type_config if isinstance(task.type_config, dict) else {}
        if _task_config_target_id(task) != target.id:
            continue
        if _is_multi_target(config):
            continue
        task.status = "paused"
        task.last_error = message
        task.hard_hourly_next_check_at = None
        task.next_run_at = None
        paused += 1
    return paused


def _is_multi_target(config: dict[str, Any]) -> bool:
    if not isinstance(config, dict):
        return False
    targets = config.get("target_operation_target_ids") or config.get("target_group_ids") or []
    return isinstance(targets, list) and len(targets) > 1


def _mark_epoch_terminal(session: Session, target: OperationTarget, revision: int, blocker: str) -> None:
    for bucket in session.scalars(
        select(TaskHardHourlyBucket).where(
            TaskHardHourlyBucket.tenant_id == target.tenant_id,
            TaskHardHourlyBucket.operation_target_id == target.id,
            TaskHardHourlyBucket.target_reference_revision == revision,
            TaskHardHourlyBucket.terminal_blocker_code == "",
        )
    ):
        bucket.terminal_blocker_code = blocker


def _sync_reactivated_target_reference(session: Session, target: OperationTarget, peer_id: str) -> None:
    group = session.scalar(
        select(TgGroup).where(
            TgGroup.tenant_id == target.tenant_id,
            TgGroup.tg_peer_id == target.tg_peer_id,
        )
    )
    if group is None:
        raise ValueError("重新激活前需存在关联目标群")
    if not peer_id or peer_id == group.tg_peer_id:
        return
    conflict = session.scalar(
        select(TgGroup.id).where(
            TgGroup.tenant_id == target.tenant_id,
            TgGroup.tg_peer_id == peer_id,
            TgGroup.id != group.id,
        )
    )
    if conflict is not None:
        raise ValueError("新引用已绑定其他目标群")
    group.tg_peer_id = peer_id


def _has_reactivated_send_capability(session: Session, target: OperationTarget) -> bool:
    group = session.scalar(
        select(TgGroup).where(
            TgGroup.tenant_id == target.tenant_id,
            TgGroup.tg_peer_id == target.tg_peer_id,
            TgGroup.can_send.is_(True),
        )
    )
    if group is None:
        return False
    return session.scalar(
        select(TgGroupAccount.id)
        .where(
            TgGroupAccount.tenant_id == target.tenant_id,
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.can_send.is_(True),
        )
        .limit(1)
    ) is not None


def _exact_username_missing(target: OperationTarget, failure_detail: str) -> bool:
    match = USERNAME_NOT_FOUND_PATTERN.search(str(failure_detail or ""))
    if match is None:
        return False
    return match.group(1).lstrip("@").lower() == str(target.username or "").lstrip("@").lower()


def _has_other_send_evidence(
    session: Session,
    target: OperationTarget,
    revision: int,
    account_id: int | None,
) -> bool:
    """True when another account/path still proves the reference is usable.

    Must not scan the entire tenant success Action table — filter by frozen target id.
    """
    group = session.scalar(
        select(TgGroup).where(
            TgGroup.tenant_id == target.tenant_id,
            TgGroup.tg_peer_id == target.tg_peer_id,
        )
    )
    if group is not None:
        other_can_send = session.scalar(
            select(TgGroupAccount.id)
            .where(
                TgGroupAccount.tenant_id == target.tenant_id,
                TgGroupAccount.group_id == group.id,
                TgGroupAccount.can_send.is_(True),
                *(
                    (TgGroupAccount.account_id != account_id,)
                    if account_id is not None
                    else ()
                ),
            )
            .limit(1)
        )
        if other_can_send is not None:
            return True
    # Prefer payload-indexed lookup; fall back to small recent window if dialect lacks JSON filter.
    targeted = session.scalar(
        select(Action.id)
        .where(
            Action.tenant_id == target.tenant_id,
            Action.status == "success",
            Action.action_type == "send_message",
            Action.payload["target_operation_target_id"].as_integer() == int(target.id),
            Action.payload["target_reference_revision"].as_integer() == int(revision),
        )
        .limit(1)
    )
    if targeted is not None:
        return True
    # Bounded fallback: only the latest 200 successes for this tenant (never full table).
    recent = session.scalars(
        select(Action)
        .where(
            Action.tenant_id == target.tenant_id,
            Action.status == "success",
            Action.action_type == "send_message",
        )
        .order_by(Action.executed_at.desc(), Action.id.desc())
        .limit(200)
    )
    for action in recent:
        payload = action.payload if isinstance(action.payload, dict) else {}
        if (
            _positive_int(payload.get("target_operation_target_id") or payload.get("operation_target_id")) == target.id
            and _positive_int(payload.get("target_reference_revision")) == int(revision)
        ):
            return True
    return False


__all__ = [
    "ERROR_TARGET_GROUP_DISSOLVED",
    "ERROR_TARGET_REF_INVALID",
    "ERROR_TARGET_REFERENCE_SUPERSEDED",
    "LIFECYCLE_ACTIVE",
    "LIFECYCLE_GROUP_DISSOLVED",
    "LIFECYCLE_TARGET_REF_INVALID",
    "MSG_GROUP_DISSOLVED",
    "MSG_TARGET_REF_INVALID",
    "TargetLifecycleImpact",
    "TargetLifecycleResult",
    "TargetTerminalBlock",
    "action_has_gateway_started",
    "auto_mark_target_ref_invalid",
    "lock_target",
    "mark_target_group_dissolved",
    "mark_target_ref_invalid",
    "preview_target_group_dissolution",
    "reactivate_target",
    "resolve_action_target",
    "skip_unstarted_target_actions",
    "terminal_target_block",
]
