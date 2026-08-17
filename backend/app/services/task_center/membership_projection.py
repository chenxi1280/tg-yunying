from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    OperationTarget,
    Task,
    TaskAdmissionProjection,
    TaskMembershipAdmissionItem,
)
from app.services._common import _now


SUMMARY_SAMPLE_LIMIT = 10


def persisted_membership_summary(
    session: Session,
    task: Task,
    channel: OperationTarget,
    *,
    require_send: bool,
) -> dict[str, Any]:
    counts = _persisted_counts(session, task)
    projection = _update_projection(session, task, counts)
    return {
        "channel_target_id": channel.id,
        "channel_title": channel.title,
        "target_type": channel.target_type,
        "subtask_type": "target_membership",
        "require_send": require_send,
        "candidate_account_count": counts["total"],
        "joined_account_count": counts["joined"],
        "need_join_account_count": counts["pending"],
        "failed_account_count": counts["failed"],
        "unknown_after_send_count": counts["unknown"],
        "blocked_account_count": counts["blocked"],
        "joined_account_ids": _phase_samples(session, task, "completed"),
        "failed_account_ids": _phase_samples(session, task, "failed"),
        "unknown_after_send_account_ids": _unknown_samples(session, task),
        "estimated_membership_actions": counts["pending"],
        "projection_revision": int(projection.item_revision or 0),
        "captured_at": projection.captured_at.isoformat(),
    }


def _persisted_counts(session: Session, task: Task) -> dict[str, int]:
    phase_counts = dict(session.execute(select(
        TaskMembershipAdmissionItem.phase,
        func.count(TaskMembershipAdmissionItem.id),
    ).where(
        TaskMembershipAdmissionItem.tenant_id == task.tenant_id,
        TaskMembershipAdmissionItem.task_id == task.id,
    ).group_by(TaskMembershipAdmissionItem.phase)).all())
    total = sum(int(value or 0) for value in phase_counts.values())
    unknown = _unknown_count(session, task)
    joined = int(phase_counts.get("completed") or 0)
    failed = max(0, int(phase_counts.get("failed") or 0) - unknown)
    pending = max(0, total - joined - failed - unknown)
    blocked = int(session.scalar(select(func.count(TaskMembershipAdmissionItem.id)).where(
        TaskMembershipAdmissionItem.tenant_id == task.tenant_id,
        TaskMembershipAdmissionItem.task_id == task.id,
        TaskMembershipAdmissionItem.manual_required.is_(True),
    )) or 0)
    return {
        "total": total,
        "joined": joined,
        "pending": pending,
        "failed": failed,
        "unknown": unknown,
        "blocked": blocked,
    }


def _unknown_count(session: Session, task: Task) -> int:
    return int(session.scalar(select(func.count(TaskMembershipAdmissionItem.id)).join(
        Action,
        Action.id == TaskMembershipAdmissionItem.membership_action_id,
    ).where(
        TaskMembershipAdmissionItem.task_id == task.id,
        Action.status == "unknown_after_send",
    )) or 0)


def _update_projection(
    session: Session,
    task: Task,
    counts: dict[str, int],
) -> TaskAdmissionProjection:
    projection = session.scalar(select(TaskAdmissionProjection).where(
        TaskAdmissionProjection.tenant_id == task.tenant_id,
        TaskAdmissionProjection.task_id == task.id,
        TaskAdmissionProjection.lifecycle_epoch == int(task.task_lifecycle_epoch or 1),
    ).with_for_update())
    values = _projection_values(counts)
    changed = projection is None
    if projection is None:
        projection = TaskAdmissionProjection(
            tenant_id=task.tenant_id,
            task_id=task.id,
            lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        )
        session.add(projection)
    else:
        changed = _current_projection_values(projection) != values
    if changed:
        projection.item_revision = int(projection.item_revision or 0) + 1
    _write_projection(projection, values)
    session.flush()
    return projection


def _projection_values(counts: dict[str, int]) -> tuple[int, ...]:
    return (
        counts["total"],
        counts["joined"],
        counts["pending"],
        counts["failed"],
        counts["unknown"],
        counts["joined"],
    )


def _current_projection_values(projection: TaskAdmissionProjection) -> tuple[int, ...]:
    return (
        projection.candidate_count,
        projection.joined_count,
        projection.pending_count,
        projection.failed_count,
        projection.unknown_count,
        projection.ready_count,
    )


def _write_projection(
    projection: TaskAdmissionProjection,
    values: tuple[int, ...],
) -> None:
    (
        projection.candidate_count,
        projection.joined_count,
        projection.pending_count,
        projection.failed_count,
        projection.unknown_count,
        projection.ready_count,
    ) = values
    projection.captured_at = _now()


def _phase_samples(session: Session, task: Task, phase: str) -> list[int]:
    return list(session.scalars(select(TaskMembershipAdmissionItem.account_id).where(
        TaskMembershipAdmissionItem.task_id == task.id,
        TaskMembershipAdmissionItem.phase == phase,
    ).order_by(TaskMembershipAdmissionItem.id).limit(SUMMARY_SAMPLE_LIMIT)))


def _unknown_samples(session: Session, task: Task) -> list[int]:
    return list(session.scalars(select(TaskMembershipAdmissionItem.account_id).join(
        Action,
        Action.id == TaskMembershipAdmissionItem.membership_action_id,
    ).where(
        TaskMembershipAdmissionItem.task_id == task.id,
        Action.status == "unknown_after_send",
    ).order_by(TaskMembershipAdmissionItem.id).limit(SUMMARY_SAMPLE_LIMIT)))


__all__ = ["persisted_membership_summary"]
