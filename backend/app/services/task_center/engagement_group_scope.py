from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OperationTarget, Task, TaskMembershipAdmissionItem, TgGroup


def group_operation_target(
    session: Session, task: Task, target_group: TgGroup
) -> OperationTarget:
    target = session.scalar(
        select(OperationTarget).where(
            OperationTarget.tenant_id == task.tenant_id,
            OperationTarget.target_type == "group",
            OperationTarget.tg_peer_id == target_group.tg_peer_id,
        )
    )
    if target is None:
        raise ValueError("group_participation_target_missing")
    return target


def sync_group_participation_scope(
    session: Session,
    task: Task,
    target_group: TgGroup,
    *,
    account_ids: list[int],
) -> int:
    target = group_operation_target(session, task, target_group)
    normalized = sorted({int(account_id) for account_id in account_ids})
    existing = set(
        session.scalars(
            select(TaskMembershipAdmissionItem.account_id).where(
                TaskMembershipAdmissionItem.task_id == task.id,
                TaskMembershipAdmissionItem.account_id.in_(normalized),
            )
        )
    )
    missing = [account_id for account_id in normalized if account_id not in existing]
    session.add_all(
        TaskMembershipAdmissionItem(
            tenant_id=task.tenant_id,
            task_id=task.id,
            account_id=account_id,
            target_id=target.id,
            phase="pending",
        )
        for account_id in missing
    )
    session.flush()
    return len(missing)


__all__ = ["group_operation_target", "sync_group_participation_scope"]
