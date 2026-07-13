from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, OperationIssue, OperationIssueSource, Task

UNRESOLVED_FAILURE_STATUSES = frozenset({"failed", "retryable_failed", "unknown_after_send"})


def get_or_create_operation_issue(
    session: Session,
    *,
    tenant_id: int,
    target_id: int | None,
    issue_type: str,
    failure_type: str,
    now_value: datetime,
) -> OperationIssue:
    issue = session.scalar(
        select(OperationIssue).where(
            OperationIssue.tenant_id == tenant_id,
            OperationIssue.target_id == target_id,
            OperationIssue.issue_type == issue_type,
            OperationIssue.failure_type == failure_type,
            OperationIssue.status == "open",
        )
    )
    if issue:
        return issue
    issue = OperationIssue(
        tenant_id=tenant_id,
        target_id=target_id,
        issue_type=issue_type,
        failure_type=failure_type,
        first_seen_at=now_value,
    )
    session.add(issue)
    session.flush()
    return issue


def issue_has_action_sources(session: Session, issue: OperationIssue) -> bool:
    return bool(
        session.scalar(
            select(OperationIssueSource.id)
            .where(
                OperationIssueSource.tenant_id == issue.tenant_id,
                OperationIssueSource.issue_id == issue.id,
                OperationIssueSource.source_type == "action",
            )
            .limit(1)
        )
    )


def has_active_unresolved_issue_actions(session: Session, issue: OperationIssue) -> bool:
    return bool(session.scalar(_active_issue_action_query(issue).with_only_columns(Action.id).limit(1)))


def active_issue_representative_action(session: Session, issue: OperationIssue) -> Action | None:
    return session.scalar(
        _active_issue_action_query(issue)
        .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
        .limit(1)
    )


def _active_issue_action_query(issue: OperationIssue):
    return (
        select(Action)
        .join(
            OperationIssueSource,
            (OperationIssueSource.source_id == Action.id)
            & (OperationIssueSource.tenant_id == issue.tenant_id)
            & (OperationIssueSource.issue_id == issue.id)
            & (OperationIssueSource.source_type == "action"),
        )
        .join(Task, Task.id == Action.task_id)
        .where(
            Action.tenant_id == issue.tenant_id,
            Action.status.in_(UNRESOLVED_FAILURE_STATUSES),
            Task.deleted_at.is_(None),
        )
    )


__all__ = [
    "UNRESOLVED_FAILURE_STATUSES",
    "active_issue_representative_action",
    "get_or_create_operation_issue",
    "has_active_unresolved_issue_actions",
    "issue_has_action_sources",
]
