from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, Task, TaskAccountDailyCoverage, TaskMembershipAdmissionItem
from app.services._common import _now


def membership_item_for_source(
    session: Session,
    task: Task,
    *,
    source_action: Action,
    account_id: int,
    target_id: int,
    lock: bool = True,
) -> TaskMembershipAdmissionItem:
    statement = select(TaskMembershipAdmissionItem).where(
        TaskMembershipAdmissionItem.tenant_id == task.tenant_id,
        TaskMembershipAdmissionItem.task_id == task.id,
        TaskMembershipAdmissionItem.account_id == account_id,
        TaskMembershipAdmissionItem.target_id == target_id,
    )
    if lock:
        statement = statement.with_for_update()
    item = session.scalar(statement)
    if item is None or not _matches_source(
        session,
        item,
        source_action=source_action,
        account_id=account_id,
        target_id=target_id,
    ):
        raise RuntimeError("admission_recovery_membership_item_drift")
    return item


def complete_member_projection(
    session: Session,
    task: Task,
    *,
    source_action: Action,
    account_id: int,
    target_id: int,
    group_id: int,
) -> None:
    item = membership_item_for_source(
        session,
        task,
        source_action=source_action,
        account_id=account_id,
        target_id=target_id,
    )
    now = _now()
    item.phase = "completed"
    item.manual_required = False
    item.failure_type = ""
    item.failure_detail = ""
    item.rescue_status = "membership_observed"
    item.rescue_failure_detail = ""
    item.completed_at = now
    item.updated_at = now
    coverages = session.scalars(_coverage_statement(task, account_id, group_id, now))
    for coverage in coverages:
        _release_coverage(coverage, now)


def _coverage_statement(task: Task, account_id: int, group_id: int, now: object):
    return select(TaskAccountDailyCoverage).where(
        TaskAccountDailyCoverage.task_id == task.id,
        TaskAccountDailyCoverage.account_id == account_id,
        TaskAccountDailyCoverage.group_id == group_id,
        TaskAccountDailyCoverage.coverage_date == now.date(),
        TaskAccountDailyCoverage.confirmed_count == 0,
        TaskAccountDailyCoverage.reserved_action_id.is_(None),
        TaskAccountDailyCoverage.state.in_(("blocked", "pending_admission")),
    ).with_for_update()


def _release_coverage(coverage: TaskAccountDailyCoverage, now: object) -> None:
    coverage.state = "ready"
    coverage.blocker_code = ""
    coverage.blocker_stage = ""
    coverage.blocker_detail = ""
    coverage.recovery_path = "membership_observed"
    coverage.next_eligible_at = None
    coverage.next_decision_at = None
    coverage.updated_at = now


def _matches_source(
    session: Session,
    item: TaskMembershipAdmissionItem,
    *,
    source_action: Action,
    account_id: int,
    target_id: int,
) -> bool:
    if item.rescue_action_id in (None, source_action.id):
        return True
    linked = session.get(Action, item.rescue_action_id)
    if linked is None:
        return False
    result = dict(linked.result or {})
    payload = dict(linked.payload or {})
    observed = (
        linked.tenant_id,
        linked.task_id,
        linked.task_lifecycle_epoch,
        linked.action_type,
        result.get("recovery_source"),
        result.get("recovery_source_action_id"),
        int(payload.get("target_account_id") or 0),
        int(payload.get("operation_target_id") or 0),
    )
    expected = (
        source_action.tenant_id,
        source_action.task_id,
        source_action.task_lifecycle_epoch,
        "invite_group_account",
        "remote_absence",
        source_action.id,
        account_id,
        target_id,
    )
    return observed == expected


__all__ = ["complete_member_projection", "membership_item_for_source"]
