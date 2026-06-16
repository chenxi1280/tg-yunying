from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountStatus, OperationTarget, Task, TaskMembershipAdmissionItem, TgAccount
from app.services._common import _now
from app.services.task_center.stats import empty_stats


PHASE_PENDING = "pending"


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


def _items_for_task(session: Session, task: Task) -> list[TaskMembershipAdmissionItem]:
    return list(
        session.scalars(
            select(TaskMembershipAdmissionItem)
            .where(TaskMembershipAdmissionItem.tenant_id == task.tenant_id, TaskMembershipAdmissionItem.task_id == task.id)
            .order_by(TaskMembershipAdmissionItem.account_id.asc())
        )
    )


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


def _refresh_snapshot_stats(task: Task, items: list[TaskMembershipAdmissionItem]) -> None:
    stats = dict(task.stats or empty_stats())
    stats["admission_snapshot_total"] = len(items)
    stats["admission_pending_count"] = sum(1 for item in items if item.phase == PHASE_PENDING)
    stats["admission_completed_count"] = 0
    stats["admission_failed_count"] = 0
    stats["admission_manual_required_count"] = 0
    task.stats = stats
