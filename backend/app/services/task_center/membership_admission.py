from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountStatus, Action, OperationTarget, Task, TaskMembershipAdmissionItem, TgAccount, TgGroup
from app.services._common import _now
from app.services.task_center.membership_recovery import AUTO_RETRY_BUCKET, GROUP_ADMIN_BUCKET, classify_membership_recovery
from app.services.task_center.payloads import DeleteMessagePayload, EnsureChannelMembershipPayload, SendMessagePayload, create_delete_action, create_membership_action, create_send_action
from app.services.task_center.stats import empty_stats


PHASE_PENDING = "pending"
PHASE_JOINING = "joining"
PHASE_TEST_MESSAGE_PENDING = "test_message_pending"
PHASE_TESTING_MESSAGE = "testing_message"
PHASE_WAITING_APPROVAL = "waiting_approval"
PHASE_FAILED = "failed"
PHASE_COMPLETED = "completed"
MEMBERSHIP_DONE_STATUSES = {"success", "failed", "skipped"}
TEST_MESSAGE_DONE_STATUSES = {"success", "failed"}
DELETE_DONE_STATUSES = {"success", "failed"}


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


def plan_membership_admission_test_messages(session: Session, task: Task, now: datetime | None = None, limit: int | None = None) -> list[Action]:
    target = _target_for_task(session, task)
    group = _group_for_target(session, task, target)
    items = _items_by_phase(session, task, PHASE_TEST_MESSAGE_PENDING, limit)
    planned_at = now or _now()
    actions: list[Action] = []
    for item in items:
        action = create_send_action(session, task, item.account_id, planned_at, _test_message_payload(task, target, group))
        item.test_message_action_id = action.id
        item.phase = PHASE_TESTING_MESSAGE
        item.updated_at = planned_at
        actions.append(action)
    _refresh_snapshot_stats(task, _items_for_task(session, task))
    session.flush()
    return actions

def plan_membership_admission_delete_messages(session: Session, task: Task, now: datetime | None = None, limit: int | None = None) -> list[Action]:
    target = _target_for_task(session, task)
    group = _group_for_target(session, task, target)
    items = _delete_pending_items(session, task, limit)
    planned_at = now or _now()
    actions: list[Action] = []
    for item in items:
        action = create_delete_action(session, task, item.account_id, planned_at, _delete_message_payload(target, group, item))
        item.delete_action_id = action.id
        item.delete_status = "deleting"
        item.updated_at = planned_at
        actions.append(action)
    session.flush()
    return actions


def sync_membership_admission_items(session: Session, task: Task, now: datetime | None = None) -> None:
    timestamp = now or _now()
    items = _items_for_task(session, task)
    membership_actions = _actions_by_id(session, [item.membership_action_id for item in items if item.membership_action_id])
    test_actions = _actions_by_id(session, [item.test_message_action_id for item in items if item.test_message_action_id])
    delete_actions = _actions_by_id(session, [item.delete_action_id for item in items if item.delete_action_id])
    accounts = _accounts_by_id(session, [item.account_id for item in items])
    for item in items:
        _sync_membership_item_if_done(item, membership_actions, accounts, timestamp)
        _sync_test_message_item_if_done(item, test_actions, timestamp)
        _sync_delete_message_item_if_done(item, delete_actions, timestamp)
    _refresh_snapshot_stats(task, items)
    session.flush()

def membership_admission_detail(session: Session, task: Task) -> tuple[dict, list[dict]]:
    if task.type != "group_membership_admission":
        return {}, []
    items = _items_for_task(session, task)
    accounts = _accounts_by_id(session, [item.account_id for item in items])
    return _admission_phase(items), [_admission_item_payload(item, accounts.get(item.account_id)) for item in items]

def retry_membership_admission_item(session: Session, tenant_id: int, task_id: str, item_id: int) -> TaskMembershipAdmissionItem:
    task, item = _task_and_item(session, tenant_id, task_id, item_id)
    _reset_item_for_retry(item, _now())
    _wake_task(task)
    _refresh_snapshot_stats(task, _items_for_task(session, task))
    session.commit()
    session.refresh(item)
    return item


def retry_failed_membership_admission_items(session: Session, tenant_id: int, task_id: str) -> int:
    task = _admission_task(session, tenant_id, task_id)
    failed_items = [item for item in _items_for_task(session, task) if item.phase == PHASE_FAILED]
    timestamp = _now()
    for item in failed_items:
        _reset_item_for_retry(item, timestamp)
    _wake_task(task)
    _refresh_snapshot_stats(task, _items_for_task(session, task))
    session.commit()
    return len(failed_items)


def mark_membership_admission_manual_handled(session: Session, tenant_id: int, task_id: str, item_id: int) -> TaskMembershipAdmissionItem:
    task, item = _task_and_item(session, tenant_id, task_id, item_id)
    _reset_item_for_retry(item, _now())
    _wake_task(task)
    _refresh_snapshot_stats(task, _items_for_task(session, task))
    session.commit()
    session.refresh(item)
    return item


def membership_admission_failure_rows(session: Session, tenant_id: int, task_id: str) -> list[dict[str, str]]:
    task = _admission_task(session, tenant_id, task_id)
    items = [item for item in _items_for_task(session, task) if item.phase == PHASE_FAILED or item.manual_required]
    accounts = _accounts_by_id(session, [item.account_id for item in items])
    return [_failure_export_row(item, accounts.get(item.account_id)) for item in items]

def _admission_task(session: Session, tenant_id: int, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if not task or task.tenant_id != tenant_id or task.type != "group_membership_admission":
        raise ValueError("群聊准入任务不存在")
    return task


def _task_and_item(session: Session, tenant_id: int, task_id: str, item_id: int) -> tuple[Task, TaskMembershipAdmissionItem]:
    task = _admission_task(session, tenant_id, task_id)
    item = session.get(TaskMembershipAdmissionItem, item_id)
    if not item or item.tenant_id != tenant_id or item.task_id != task.id:
        raise ValueError("群聊准入账号项不存在")
    return task, item


def _failure_export_row(item: TaskMembershipAdmissionItem, account: TgAccount | None) -> dict[str, str]:
    return {
        "account_id": str(item.account_id),
        "display_name": account.display_name if account else f"账号 #{item.account_id}",
        "username": account.username if account else "",
        "phase": item.phase,
        "manual_required": "true" if item.manual_required else "false",
        "failure_type": item.failure_type,
        "failure_detail": item.failure_detail,
        "test_message_id": item.test_message_id,
        "delete_status": item.delete_status,
    }


def _reset_item_for_retry(item: TaskMembershipAdmissionItem, timestamp: datetime) -> None:
    item.phase = PHASE_PENDING
    item.membership_action_id = None
    item.test_message_action_id = None
    item.delete_action_id = None
    item.delete_status = ""
    item.manual_required = False
    item.failure_type = ""
    item.failure_detail = ""
    item.completed_at = None
    item.updated_at = timestamp


def _wake_task(task: Task) -> None:
    if task.status != "paused":
        task.status = "running"
        task.next_run_at = _now()
    task.last_error = ""


def _items_for_task(session: Session, task: Task) -> list[TaskMembershipAdmissionItem]:
    return list(
        session.scalars(
            select(TaskMembershipAdmissionItem)
            .where(TaskMembershipAdmissionItem.tenant_id == task.tenant_id, TaskMembershipAdmissionItem.task_id == task.id)
            .order_by(TaskMembershipAdmissionItem.account_id.asc())
        )
    )


def _admission_phase(items: list[TaskMembershipAdmissionItem]) -> dict:
    return {
        "snapshot_total": len(items),
        "pending_count": sum(1 for item in items if item.phase == PHASE_PENDING),
        "joining_count": sum(1 for item in items if item.phase == PHASE_JOINING),
        "test_message_pending_count": sum(1 for item in items if item.phase == PHASE_TEST_MESSAGE_PENDING),
        "testing_message_count": sum(1 for item in items if item.phase == PHASE_TESTING_MESSAGE),
        "completed_count": sum(1 for item in items if item.phase == PHASE_COMPLETED),
        "failed_count": sum(1 for item in items if item.phase == PHASE_FAILED),
        "manual_required_count": sum(1 for item in items if item.manual_required),
        "completed": bool(items) and all(item.phase == PHASE_COMPLETED for item in items),
    }


def _admission_item_payload(item: TaskMembershipAdmissionItem, account: TgAccount | None) -> dict:
    return {
        "id": item.id,
        "account_id": item.account_id,
        "display_name": account.display_name if account else f"账号 #{item.account_id}",
        "username": account.username if account else "",
        "target_id": item.target_id,
        "phase": item.phase,
        "membership_action_id": item.membership_action_id,
        "test_message_action_id": item.test_message_action_id,
        "delete_action_id": item.delete_action_id,
        "test_message_text": item.test_message_text,
        "test_message_id": item.test_message_id,
        "delete_after_send": item.delete_after_send,
        "delete_status": item.delete_status,
        "failure_type": item.failure_type,
        "failure_detail": item.failure_detail,
        "manual_required": item.manual_required,
        "completed_at": item.completed_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _pending_items(session: Session, task: Task, limit: int | None) -> list[TaskMembershipAdmissionItem]:
    return _items_by_phase(session, task, PHASE_PENDING, limit)


def _delete_pending_items(session: Session, task: Task, limit: int | None) -> list[TaskMembershipAdmissionItem]:
    stmt = (
        select(TaskMembershipAdmissionItem)
        .where(
            TaskMembershipAdmissionItem.tenant_id == task.tenant_id,
            TaskMembershipAdmissionItem.task_id == task.id,
            TaskMembershipAdmissionItem.phase == PHASE_COMPLETED,
            TaskMembershipAdmissionItem.delete_after_send.is_(True),
            TaskMembershipAdmissionItem.delete_status == "delete_pending",
        )
        .order_by(TaskMembershipAdmissionItem.account_id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def _items_by_phase(session: Session, task: Task, phase: str, limit: int | None) -> list[TaskMembershipAdmissionItem]:
    stmt = (
        select(TaskMembershipAdmissionItem)
        .where(TaskMembershipAdmissionItem.tenant_id == task.tenant_id, TaskMembershipAdmissionItem.task_id == task.id, TaskMembershipAdmissionItem.phase == phase)
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


def _group_for_target(session: Session, task: Task, target: OperationTarget) -> TgGroup:
    group = session.scalar(
        select(TgGroup).where(TgGroup.tenant_id == task.tenant_id, TgGroup.tg_peer_id == target.tg_peer_id).limit(1)
    )
    if not group:
        raise ValueError("群聊准入任务目标群未同步到本地群表，无法发送测试消息")
    return group


def _test_message_payload(task: Task, target: OperationTarget, group: TgGroup) -> SendMessagePayload:
    return SendMessagePayload(
        group_id=group.id,
        operation_target_id=target.id,
        target_display=target.title,
        message_text="",
        ai_generation_status="pending",
        ai_generation_id=f"{task.id}:membership-admission-test",
        ai_generation_count=1,
        profile_scene="group_membership_admission_test",
    )


def _delete_message_payload(target: OperationTarget, group: TgGroup, item: TaskMembershipAdmissionItem) -> DeleteMessagePayload:
    return DeleteMessagePayload(
        group_id=group.id,
        chat_id=str(group.tg_peer_id or target.tg_peer_id or ""),
        operation_target_id=target.id,
        target_display=target.title,
        message_id=item.test_message_id,
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


def _sync_membership_item_if_done(
    item: TaskMembershipAdmissionItem,
    actions: dict[str, Action],
    accounts: dict[int, TgAccount],
    timestamp: datetime,
) -> None:
    action = actions.get(item.membership_action_id or "")
    if action and action.status in MEMBERSHIP_DONE_STATUSES:
        _sync_membership_item(item, action, accounts.get(item.account_id), timestamp)


def _sync_test_message_item_if_done(item: TaskMembershipAdmissionItem, actions: dict[str, Action], timestamp: datetime) -> None:
    action = actions.get(item.test_message_action_id or "")
    if not action or action.status not in TEST_MESSAGE_DONE_STATUSES:
        return
    if action.status == "success" and bool((action.result or {}).get("success")):
        _mark_completed(item, action, timestamp)
        return
    item.phase = PHASE_FAILED
    item.failure_type = "test_message_failed"
    item.failure_detail = str((action.result or {}).get("error_message") or (action.result or {}).get("detail") or "测试发言失败")
    item.updated_at = timestamp


def _sync_delete_message_item_if_done(item: TaskMembershipAdmissionItem, actions: dict[str, Action], timestamp: datetime) -> None:
    action = actions.get(item.delete_action_id or "")
    if not action or action.status not in DELETE_DONE_STATUSES:
        return
    if action.status == "success" and bool((action.result or {}).get("success")):
        item.delete_status = "deleted"
        item.failure_type = ""
        item.failure_detail = ""
    else:
        item.delete_status = "delete_failed"
        item.failure_type = "delete_message_failed"
        item.failure_detail = str((action.result or {}).get("error_message") or "删除测试消息失败")
    item.updated_at = timestamp


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


def _mark_completed(item: TaskMembershipAdmissionItem, action: Action, timestamp: datetime) -> None:
    item.phase = PHASE_COMPLETED
    item.test_message_text = str((action.payload or {}).get("message_text") or "")
    item.test_message_id = str((action.result or {}).get("telegram_msg_id") or "")
    item.delete_action_id = None
    item.delete_status = "not_requested" if not item.delete_after_send else "delete_pending"
    item.failure_type = ""
    item.failure_detail = ""
    item.manual_required = False
    item.completed_at = timestamp
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
    stats["admission_testing_message_count"] = sum(1 for item in items if item.phase == PHASE_TESTING_MESSAGE)
    stats["admission_completed_count"] = sum(1 for item in items if item.completed_at is not None)
    stats["admission_failed_count"] = sum(1 for item in items if item.phase == PHASE_FAILED)
    stats["admission_manual_required_count"] = sum(1 for item in items if item.manual_required)
    task.stats = stats
    if items and stats["admission_completed_count"] == len(items):
        task.status = "completed"
