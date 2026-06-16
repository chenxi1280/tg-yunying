from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountStatus, Action, OperationTarget, Task, TaskMembershipAdmissionItem, TgAccount
from app.services._common import _now
from app.services.task_center.membership_recovery import AUTO_RETRY_BUCKET, GROUP_ADMIN_BUCKET, classify_membership_recovery
from app.services.task_center.payloads import EnsureChannelMembershipPayload, create_membership_action
from app.services.task_center.stats import empty_stats


PHASE_PENDING = "pending"
PHASE_JOINING = "joining"
PHASE_TEST_MESSAGE_PENDING = "test_message_pending"
PHASE_WAITING_APPROVAL = "waiting_approval"
PHASE_FAILED = "failed"
MEMBERSHIP_DONE_STATUSES = {"success", "failed", "skipped"}


def lock_membership_admission_snapshot(session: Session, task: Task, now: datetime | None = None) -> list[TaskMembershipAdmissionItem]:
    existing = _items_for_task(session, task)
    if existing:
        return existing
    target_id = int(task.type_config.get("target_operation_target_id") or 0)
    if not target_id:
        raise ValueError("群聊准入任务缺少目标群聊")
    target = session.get(OperationTarget, target_id)
    if not target or target.tenant_id != task.tenant_id or target.target_type != "group":
        raise ValueError("群聊准入任务目标群聊不存在或类型不匹配")
    account_ids = _snapshot_account_ids(session, task)
    timestamp = now or _now()
    items = [
        TaskMembershipAdmissionItem(
            tenant_id=task.tenant_id,
            task_id=task.id,
            account_id=account_id,
            target_id=target.id,
            phase=PHASE_PENDING,
            delete_after_send=bool((task.type_config.get("test_message") or {}).get("delete_after_send")),
            created_at=timestamp,
            updated_at=timestamp,
        )
        for account_id in account_ids
    ]
    session.add_all(items)
    _refresh_snapshot_stats(task, items)
    session.flush()
    return items


def plan_membership_admission_actions(session: Session, task: Task, now: datetime | None = None, limit: int | None = None) -> list[Action]:
    target = _target_for_task(session, task)
    pending_items = _pending_items(session, task, limit)
    planned_at = now or _now()
    actions: list[Action] = []
    for item in pending_items:
        action = create_membership_action(session, task, item.account_id, planned_at, _membership_payload(target), flush=True)
        item.membership_action_id = action.id
        item.phase = PHASE_JOINING
        item.updated_at = planned_at
        actions.append(action)
    _refresh_snapshot_stats(task, _items_for_task(session, task))
    session.flush()
    return actions


def sync_membership_admission_items(session: Session, task: Task, now: datetime | None = None) -> None:
    timestamp = now or _now()
    items = _items_for_task(session, task)
    actions = _actions_by_id(session, [item.membership_action_id for item in items if item.membership_action_id])
    accounts = _accounts_by_id(session, [item.account_id for item in items])
    for item in items:
        action = actions.get(item.membership_action_id or "")
        if not action or action.status not in MEMBERSHIP_DONE_STATUSES:
            continue
        _sync_membership_item(item, action, accounts.get(item.account_id), timestamp)
    _refresh_snapshot_stats(task, items)
    session.flush()


def _items_for_task(session: Session, task: Task) -> list[TaskMembershipAdmissionItem]:
    return list(
        session.scalars(
            select(TaskMembershipAdmissionItem)
            .where(TaskMembershipAdmissionItem.tenant_id == task.tenant_id, TaskMembershipAdmissionItem.task_id == task.id)
            .order_by(TaskMembershipAdmissionItem.account_id.asc())
        )
    )


def _pending_items(session: Session, task: Task, limit: int | None) -> list[TaskMembershipAdmissionItem]:
    stmt = (
        select(TaskMembershipAdmissionItem)
        .where(TaskMembershipAdmissionItem.tenant_id == task.tenant_id, TaskMembershipAdmissionItem.task_id == task.id, TaskMembershipAdmissionItem.phase == PHASE_PENDING)
        .order_by(TaskMembershipAdmissionItem.account_id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def _target_for_task(session: Session, task: Task) -> OperationTarget:
    target_id = int(task.type_config.get("target_operation_target_id") or 0)
    target = session.get(OperationTarget, target_id)
    if not target or target.tenant_id != task.tenant_id or target.target_type != "group":
        raise ValueError("群聊准入任务目标群聊不存在或类型不匹配")
    return target


def _membership_payload(target: OperationTarget) -> EnsureChannelMembershipPayload:
    username = str(target.username or "")
    return EnsureChannelMembershipPayload(
        channel_id=str(target.tg_peer_id or username),
        channel_target_id=target.id,
        target_type="group",
        target_display=target.title,
        target_username=username,
        invite_link=username if _looks_like_invite_ref(username) else "",
        require_send=True,
    )


def _looks_like_invite_ref(value: str) -> bool:
    text = (value or "").strip()
    return text.startswith(("+", "https://t.me/+", "http://t.me/+", "t.me/+"))


def _snapshot_account_ids(session: Session, task: Task) -> list[int]:
    group_ids = [int(item) for item in task.type_config.get("account_group_ids") or []]
    if not group_ids:
        raise ValueError("群聊准入任务缺少账号分组")
    return list(
        session.scalars(
            select(TgAccount.id)
            .where(
                TgAccount.tenant_id == task.tenant_id,
                TgAccount.deleted_at.is_(None),
                TgAccount.status == AccountStatus.ACTIVE.value,
                TgAccount.pool_id.in_(group_ids),
            )
            .order_by(TgAccount.id.asc())
        )
    )


def _actions_by_id(session: Session, action_ids: list[str | None]) -> dict[str, Action]:
    ids = [str(action_id) for action_id in action_ids if action_id]
    if not ids:
        return {}
    return {action.id: action for action in session.scalars(select(Action).where(Action.id.in_(ids)))}


def _accounts_by_id(session: Session, account_ids: list[int]) -> dict[int, TgAccount]:
    ids = sorted({int(account_id) for account_id in account_ids})
    if not ids:
        return {}
    return {account.id: account for account in session.scalars(select(TgAccount).where(TgAccount.id.in_(ids)))}


def _sync_membership_item(item: TaskMembershipAdmissionItem, action: Action, account: TgAccount | None, timestamp: datetime) -> None:
    if _membership_action_success(action):
        _mark_test_message_pending(item, timestamp)
        return
    recovery = classify_membership_recovery(
        phase=_action_phase(action),
        account_status=account.status if account else "",
        action_status=action.status,
        failure_type=str((action.result or {}).get("error_code") or ""),
        failure_detail=str((action.result or {}).get("error_message") or (action.result or {}).get("detail") or ""),
        verification_action="",
        verification_status="",
        can_auto_resolve=False,
    )
    if recovery.bucket == AUTO_RETRY_BUCKET:
        _mark_pending_retry(item, timestamp)
        return
    item.phase = PHASE_WAITING_APPROVAL if recovery.bucket == GROUP_ADMIN_BUCKET else PHASE_FAILED
    item.manual_required = bool(recovery.operator_required or recovery.account_replace_required)
    item.failure_type = recovery.bucket
    item.failure_detail = str((action.result or {}).get("error_message") or (action.result or {}).get("detail") or recovery.label)
    item.updated_at = timestamp


def _membership_action_success(action: Action) -> bool:
    result = action.result or {}
    return result.get("membership_status") in {"joined", "already_joined"} or (action.status == "success" and bool(result.get("success")))


def _mark_test_message_pending(item: TaskMembershipAdmissionItem, timestamp: datetime) -> None:
    item.phase = PHASE_TEST_MESSAGE_PENDING
    item.manual_required = False
    item.failure_type = ""
    item.failure_detail = ""
    item.updated_at = timestamp


def _mark_pending_retry(item: TaskMembershipAdmissionItem, timestamp: datetime) -> None:
    item.phase = PHASE_PENDING
    item.membership_action_id = None
    item.failure_type = ""
    item.failure_detail = ""
    item.updated_at = timestamp


def _action_phase(action: Action) -> str:
    result = action.result or {}
    if result.get("membership_status") == "permission_denied":
        return "manual_required"
    return PHASE_FAILED if action.status == "failed" else action.status


def _refresh_snapshot_stats(task: Task, items: list[TaskMembershipAdmissionItem]) -> None:
    stats = dict(task.stats or empty_stats())
    stats["admission_snapshot_total"] = len(items)
    stats["admission_pending_count"] = sum(1 for item in items if item.phase == PHASE_PENDING)
    stats["admission_joining_count"] = sum(1 for item in items if item.phase == PHASE_JOINING)
    stats["admission_test_message_pending_count"] = sum(1 for item in items if item.phase == PHASE_TEST_MESSAGE_PENDING)
    stats["admission_completed_count"] = sum(1 for item in items if item.completed_at is not None)
    stats["admission_failed_count"] = sum(1 for item in items if item.phase == PHASE_FAILED)
    stats["admission_manual_required_count"] = sum(1 for item in items if item.manual_required)
    task.stats = stats
