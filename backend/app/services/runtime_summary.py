from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountRuntimeSummary,
    Action,
    MessageTask,
    MessageTaskAttempt,
    OperationIssue,
    OperationIssueAccount,
    OperationIssueSource,
    OperationTarget,
    TargetRuntimeSummary,
    Task,
    TaskRuntimeSummary,
    TgAccount,
    TgGroup,
    TgAccountSecurityBatchItem,
    TgAccountSecuritySnapshot,
)
from app.models.enums import AccountStatus
from app.models.enums import FailureType
from app.services._common import _now, audit
from app.services.account_capacity import account_capacity_decision


PENDING_STATUSES = {"pending", "claiming", "executing", "retryable_failed", "unknown_after_send"}
UNRESOLVED_FAILURE_STATUSES = {"failed", "retryable_failed", "unknown_after_send"}
RISK_SIGNAL_STATUSES = {"failed", "skipped", "retryable_failed", "unknown_after_send"}
SECURITY_RETRY_STATUSES = {"pending", "waiting", "failed", "partial_success"}
FAILURE_TYPE_CODES = {item.value: item.name for item in FailureType}


def refresh_task_summary(session: Session, task: Task) -> TaskRuntimeSummary:
    session.flush()
    rows = session.execute(select(Action.status, func.count(Action.id)).where(Action.task_id == task.id).group_by(Action.status)).all()
    counts = {str(status): int(count) for status, count in rows}
    oldest_pending = session.scalar(select(func.min(Action.scheduled_at)).where(Action.task_id == task.id, Action.status.in_(PENDING_STATUSES)))
    latest_failure = session.scalar(
        select(Action)
        .where(Action.task_id == task.id, Action.status == "failed")
        .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
        .limit(1)
    )
    target_id = _task_target_id(task)
    summary = _get_or_create_task_summary(session, task.tenant_id, task.id)
    summary.task_status = task.status
    summary.target_id = target_id
    summary.planned_count = sum(counts.values())
    summary.success_count = counts.get("success", 0)
    summary.failed_count = counts.get("failed", 0)
    summary.pending_count = sum(counts.get(status, 0) for status in PENDING_STATUSES)
    summary.oldest_pending_at = oldest_pending
    summary.latest_failure_type = _failure_type(latest_failure)
    summary.summary = {
        "counts": counts,
        "task_type": task.type,
        "target_summary": (task.stats or {}).get("target_summary") or "",
    }
    summary.updated_at = _now()
    if latest_failure:
        latest_failure_type = summary.latest_failure_type
        upsert_operation_issue(
            session,
            tenant_id=task.tenant_id,
            target_id=target_id,
            issue_type=_issue_type(latest_failure),
            failure_type=latest_failure_type,
            source_task_id=task.id,
            representative_action_id=latest_failure.id,
            affected_account_ids=[latest_failure.account_id] if latest_failure.account_id else [],
            failure_reason=_failure_reason(latest_failure),
            suggested_action=_suggested_action(latest_failure_type),
            handling_mode=_handling_mode(latest_failure_type),
        )
    elif not any(counts.get(status, 0) for status in UNRESOLVED_FAILURE_STATUSES):
        _resolve_task_issues_if_recovered(session, task)
    if target_id:
        refresh_target_summary(session, task.tenant_id, target_id)
    for account_id in _task_account_ids(task, latest_failure):
        if session.get(TgAccount, account_id):
            refresh_account_summary(session, task.tenant_id, account_id)
    return summary


def rollup_message_task_failure(session: Session, task: MessageTask) -> OperationIssue | None:
    if task.status != "失败":
        return None
    failure_type = str(task.failure_type or FailureType.UNKNOWN.value)
    target_id = _message_task_target_id(session, task)
    latest_attempt = session.scalar(
        select(MessageTaskAttempt)
        .where(MessageTaskAttempt.tenant_id == task.tenant_id, MessageTaskAttempt.task_id == task.id)
        .order_by(MessageTaskAttempt.created_at.desc(), MessageTaskAttempt.id.desc())
        .limit(1)
    )
    representative_id = f"message_task_attempt:{latest_attempt.id}" if latest_attempt else f"message_task:{task.id}"
    now_value = _now()
    issue = session.scalar(
        select(OperationIssue).where(
            OperationIssue.tenant_id == task.tenant_id,
            OperationIssue.target_id == target_id,
            OperationIssue.issue_type == "message_send_failure",
            OperationIssue.failure_type == failure_type,
            OperationIssue.status == "open",
        )
    )
    if not issue:
        issue = OperationIssue(
            tenant_id=task.tenant_id,
            target_id=target_id,
            issue_type="message_send_failure",
            failure_type=failure_type,
            first_seen_at=now_value,
        )
        session.add(issue)
        session.flush()

    known_accounts = set(int(item) for item in (issue.affected_account_ids or []) if item)
    if task.account_id:
        known_accounts.add(int(task.account_id))
    issue.severity = "warning"
    issue.source_task_id = f"message_task:{task.id}"
    issue.representative_action_id = representative_id
    issue.affected_account_ids = sorted(known_accounts)
    issue.failure_reason = str(task.failure_detail or (latest_attempt.detail if latest_attempt else "") or failure_type)
    issue.suggested_action = _message_task_suggested_action(failure_type)
    issue.handling_mode = _handling_mode(failure_type)
    issue.return_to = {
        "page": "message-sending",
        "source_issue_id": issue.id,
        "target_id": target_id,
        "message_task_id": task.id,
        "default_tab": "records",
        "filters": {
            "target_id": target_id,
            "message_task_id": task.id,
            "failure_type": failure_type,
            "status": issue.status,
        },
    }
    issue.last_seen_at = now_value
    issue.updated_at = now_value
    issue.summary = {
        **(issue.summary or {}),
        "hit_count": int((issue.summary or {}).get("hit_count") or 0) + 1,
        "target_display": task.target_display or "",
        "message_task_status": task.status,
    }
    _upsert_issue_source(
        session,
        task.tenant_id,
        issue.id,
        "message_task",
        str(task.id),
        failure_type,
        now_value,
        {
            "message_task_id": task.id,
            "target_type": task.target_type,
            "target_display": task.target_display or "",
            "status": task.status,
            "failure_reason": issue.failure_reason,
        },
    )
    if latest_attempt:
        _upsert_issue_source(
            session,
            task.tenant_id,
            issue.id,
            "message_task_attempt",
            str(latest_attempt.id),
            failure_type,
            now_value,
            {"message_task_id": task.id, "account_id": latest_attempt.account_id, "detail": latest_attempt.detail or ""},
        )
    for account_id in known_accounts:
        _upsert_issue_account(
            session,
            task.tenant_id,
            issue.id,
            account_id,
            "message_send_failure",
            now_value,
            {"message_task_id": task.id, "target_id": target_id},
        )
    issue.affected_task_count = _issue_source_count(session, task.tenant_id, issue.id, "message_task")
    issue.affected_account_count = _issue_account_count(session, task.tenant_id, issue.id)
    if target_id:
        refresh_target_summary(session, task.tenant_id, target_id)
    return issue


def resolve_message_task_issues_if_recovered(session: Session, task: MessageTask) -> None:
    if task.status != "已发送":
        return
    now_value = _now()
    issues = list(
        session.scalars(
            select(OperationIssue)
            .join(OperationIssueSource, OperationIssueSource.issue_id == OperationIssue.id)
            .where(
                OperationIssue.tenant_id == task.tenant_id,
                OperationIssue.status == "open",
                OperationIssueSource.tenant_id == task.tenant_id,
                OperationIssueSource.source_type == "message_task",
                OperationIssueSource.source_id == str(task.id),
            )
        )
    )
    for issue in issues:
        if _issue_has_failed_message_task_source(session, task.tenant_id, issue.id):
            continue
        issue.status = "resolved"
        issue.resolved_at = now_value
        issue.updated_at = now_value
        issue.summary = {
            **(issue.summary or {}),
            "resolve_reason": "消息发送任务恢复后已发送",
            "resolved_by": "system",
            "auto_resolved": True,
        }
        audit(session, tenant_id=task.tenant_id, actor="system", action="自动解决消息发送运营异常", target_type="operation_issue", target_id=issue.id, detail="消息发送任务恢复后已发送")
        if issue.target_id:
            refresh_target_summary(session, task.tenant_id, issue.target_id)


def refresh_target_summary(session: Session, tenant_id: int, target_id: int) -> TargetRuntimeSummary:
    rows = list(session.scalars(select(TaskRuntimeSummary).where(TaskRuntimeSummary.tenant_id == tenant_id, TaskRuntimeSummary.target_id == target_id)))
    task_ids = [row.task_id for row in rows]
    open_issue_count = session.scalar(select(func.count(OperationIssue.id)).where(OperationIssue.tenant_id == tenant_id, OperationIssue.target_id == target_id, OperationIssue.status == "open")) or 0
    failed_action_count = sum(int(row.failed_count or 0) for row in rows)
    affected_task_count = len({row.task_id for row in rows if int(row.failed_count or 0) > 0})
    latest_failure_at = (
        session.scalar(
            select(func.max(Action.executed_at)).where(
                Action.tenant_id == tenant_id,
                Action.status == "failed",
                Action.task_id.in_(task_ids),
            )
        )
        if task_ids
        else None
    )
    summary = _get_or_create_target_summary(session, tenant_id, target_id)
    summary.status = "issue_open" if open_issue_count else "failed" if failed_action_count else "healthy"
    summary.open_issue_count = int(open_issue_count or 0)
    summary.failed_action_count = int(failed_action_count or 0)
    summary.affected_task_count = int(affected_task_count or 0)
    summary.latest_failure_at = latest_failure_at
    summary.summary = {
        "task_count": len(rows),
        "status_counts": _task_status_counts(rows),
    }
    summary.updated_at = _now()
    return summary


def refresh_account_summary(session: Session, tenant_id: int, account_id: int) -> AccountRuntimeSummary:
    account = session.get(TgAccount, account_id)
    summary = _get_or_create_account_summary(session, tenant_id, account_id)
    recent_cutoff = _now() - timedelta(hours=24)
    rows = session.execute(
        select(Action.status, func.count(Action.id))
        .where(Action.tenant_id == tenant_id, Action.account_id == account_id, Action.created_at >= recent_cutoff)
        .group_by(Action.status)
    ).all()
    trend = {str(status): int(count) for status, count in rows}
    pending_count = sum(trend.get(status, 0) for status in PENDING_STATUSES)
    is_active = bool(account and account.status == AccountStatus.ACTIVE.value and not account.deleted_at and account.session_ciphertext)
    unavailable_reason = "" if is_active else _account_unavailable_reason(account)
    next_retry_at = None
    capacity_available = True
    rate_limit_next_retry_at, rate_limit_reason, rate_limit_count = _account_rate_limit_signal(session, tenant_id, account_id, recent_cutoff)
    security_blocked, security_reason, security_trend = _account_security_signal(session, tenant_id, account_id)
    security_retry_at = _account_security_next_retry_at(session, tenant_id, account_id)
    recent_risk_trend = _account_recent_risk_signal(session, tenant_id, account_id, recent_cutoff)
    if is_active:
        capacity_decision = account_capacity_decision(session, tenant_id=tenant_id, account_id=account_id)
        capacity_available = capacity_decision.available
        next_retry_at = capacity_decision.defer_until
        unavailable_reason = _account_proxy_unavailable_reason(account) or (
            "" if capacity_decision.available else capacity_decision.reason_code or capacity_decision.reason
        )
    if rate_limit_next_retry_at and rate_limit_next_retry_at > _now():
        next_retry_at = _later_retry_at(next_retry_at, rate_limit_next_retry_at)
        if not unavailable_reason:
            unavailable_reason = rate_limit_reason
    if rate_limit_count:
        trend["rate_limit_count"] = rate_limit_count
        trend["rate_limit_reason"] = rate_limit_reason
    if security_retry_at and security_retry_at > _now():
        next_retry_at = _later_retry_at(next_retry_at, security_retry_at)
        if not unavailable_reason:
            unavailable_reason = security_reason or "security_retry_waiting"
    if security_trend:
        trend.update(security_trend)
    if recent_risk_trend:
        trend.update(recent_risk_trend)
    remaining_capacity = max(0, 100 - int(pending_count or 0))
    capability_available = is_active and not _account_proxy_unavailable_reason(account)
    if security_blocked:
        capability_available = False
        if not unavailable_reason:
            unavailable_reason = security_reason
    summary.send_available = capability_available and capacity_available and remaining_capacity > 0
    summary.listen_available = capability_available
    summary.join_available = capability_available
    summary.comment_available = capability_available
    summary.profile_available = capability_available
    summary.code_read_available = capability_available
    summary.remaining_capacity = remaining_capacity
    summary.unavailable_reason = unavailable_reason
    summary.next_retry_at = next_retry_at
    summary.failure_trend = trend
    summary.updated_at = _now()
    return summary


def upsert_operation_issue(
    session: Session,
    *,
    tenant_id: int,
    target_id: int | None,
    issue_type: str,
    failure_type: str,
    source_task_id: str,
    representative_action_id: str,
    affected_account_ids: Iterable[int],
    failure_reason: str,
    suggested_action: str,
    severity: str = "warning",
    handling_mode: str = "modal",
) -> OperationIssue:
    now_value = _now()
    issue = session.scalar(
        select(OperationIssue).where(
            OperationIssue.tenant_id == tenant_id,
            OperationIssue.target_id == target_id,
            OperationIssue.issue_type == issue_type,
            OperationIssue.failure_type == failure_type,
            OperationIssue.status == "open",
        )
    )
    if not issue:
        issue = OperationIssue(
            tenant_id=tenant_id,
            target_id=target_id,
            issue_type=issue_type,
            failure_type=failure_type,
            first_seen_at=now_value,
        )
        session.add(issue)
        session.flush()
    known_accounts = set(int(item) for item in (issue.affected_account_ids or []) if item)
    known_accounts.update(int(item) for item in affected_account_ids if item)
    issue.severity = severity
    issue.source_task_id = source_task_id
    issue.representative_action_id = representative_action_id
    issue.affected_task_count = _issue_source_count(session, tenant_id, issue.id, "task") + (0 if _issue_has_source(session, tenant_id, issue.id, "task", source_task_id) else 1)
    issue.affected_account_count = len(known_accounts)
    issue.affected_account_ids = sorted(known_accounts)
    issue.failure_reason = failure_reason
    issue.suggested_action = suggested_action
    issue.handling_mode = handling_mode
    issue.return_to = _issue_return_to(issue, source_task_id=source_task_id, representative_action_id=representative_action_id)
    issue.last_seen_at = now_value
    issue.updated_at = now_value
    issue.summary = {**(issue.summary or {}), "hit_count": int((issue.summary or {}).get("hit_count") or 0) + 1}
    _upsert_issue_source(session, tenant_id, issue.id, "task", source_task_id, failure_type, now_value, {"representative_action_id": representative_action_id})
    _upsert_issue_source(session, tenant_id, issue.id, "action", representative_action_id, failure_type, now_value, {"source_task_id": source_task_id})
    for account_id in known_accounts:
        _upsert_issue_account(session, tenant_id, issue.id, account_id, "execution_failure", now_value, {"source_task_id": source_task_id})
    issue.affected_task_count = _issue_source_count(session, tenant_id, issue.id, "task")
    issue.affected_account_count = _issue_account_count(session, tenant_id, issue.id)
    return issue


def resolve_operation_issue(session: Session, tenant_id: int, issue_id: str, reason: str = "", actor: str = "system") -> OperationIssue:
    issue = session.get(OperationIssue, issue_id)
    if not issue or issue.tenant_id != tenant_id:
        raise ValueError("operation issue not found")
    issue.status = "resolved"
    issue.resolved_at = _now()
    issue.updated_at = _now()
    issue.summary = {**(issue.summary or {}), "resolve_reason": reason, "resolved_by": actor}
    audit(session, tenant_id=tenant_id, actor=actor, action="解决运营异常", target_type="operation_issue", target_id=issue.id, detail=reason)
    if issue.target_id:
        refresh_target_summary(session, tenant_id, issue.target_id)
    return issue


def _resolve_task_issues_if_recovered(session: Session, task: Task) -> None:
    now_value = _now()
    issues = list(
        session.scalars(
            select(OperationIssue).where(
                OperationIssue.tenant_id == task.tenant_id,
                OperationIssue.source_task_id == task.id,
                OperationIssue.status == "open",
            )
        )
    )
    for issue in issues:
        issue.status = "resolved"
        issue.resolved_at = now_value
        issue.updated_at = now_value
        issue.summary = {
            **(issue.summary or {}),
            "resolve_reason": "任务恢复后未发现失败执行项",
            "resolved_by": "system",
            "auto_resolved": True,
        }
        audit(session, tenant_id=task.tenant_id, actor="system", action="自动解决运营异常", target_type="operation_issue", target_id=issue.id, detail="任务恢复后未发现失败执行项")


def rebuild_runtime_summaries(session: Session, tenant_id: int, scope: str = "all") -> dict[str, int]:
    result = {"tasks": 0, "targets": 0, "accounts": 0}
    if scope in {"all", "tasks"}:
        for task in session.scalars(select(Task).where(Task.tenant_id == tenant_id, Task.deleted_at.is_(None))):
            refresh_task_summary(session, task)
            result["tasks"] += 1
    if scope in {"all", "targets"}:
        for target_id in session.scalars(select(OperationTarget.id).where(OperationTarget.tenant_id == tenant_id)):
            refresh_target_summary(session, tenant_id, target_id)
            result["targets"] += 1
    if scope in {"all", "accounts"}:
        for account_id in session.scalars(select(TgAccount.id).where(TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None))):
            refresh_account_summary(session, tenant_id, account_id)
            result["accounts"] += 1
    return result


def list_account_runtime_summaries(session: Session, tenant_id: int) -> list[AccountRuntimeSummary]:
    return list(session.scalars(select(AccountRuntimeSummary).where(AccountRuntimeSummary.tenant_id == tenant_id).order_by(AccountRuntimeSummary.updated_at.desc())))


def list_target_runtime_summaries(session: Session, tenant_id: int) -> list[TargetRuntimeSummary]:
    return list(session.scalars(select(TargetRuntimeSummary).where(TargetRuntimeSummary.tenant_id == tenant_id).order_by(TargetRuntimeSummary.updated_at.desc())))


def get_account_runtime_summary(session: Session, tenant_id: int, account_id: int) -> AccountRuntimeSummary:
    summary = session.scalar(select(AccountRuntimeSummary).where(AccountRuntimeSummary.tenant_id == tenant_id, AccountRuntimeSummary.account_id == account_id))
    if summary:
        return summary
    return refresh_account_summary(session, tenant_id, account_id)


def operation_center_overview(session: Session, tenant_id: int) -> dict[str, Any]:
    open_issues = list(session.scalars(select(OperationIssue).where(OperationIssue.tenant_id == tenant_id, OperationIssue.status == "open")))
    latest_values = [
        session.scalar(select(func.max(TargetRuntimeSummary.updated_at)).where(TargetRuntimeSummary.tenant_id == tenant_id)),
        session.scalar(select(func.max(TaskRuntimeSummary.updated_at)).where(TaskRuntimeSummary.tenant_id == tenant_id)),
        session.scalar(select(func.max(AccountRuntimeSummary.updated_at)).where(AccountRuntimeSummary.tenant_id == tenant_id)),
        session.scalar(select(func.max(OperationIssue.updated_at)).where(OperationIssue.tenant_id == tenant_id)),
    ]
    latest_updated_at = max([value for value in latest_values if value is not None], default=None)
    affected_accounts: set[int] = set()
    for issue in open_issues:
        affected_accounts.update(int(item) for item in (issue.affected_account_ids or []) if item)
    running_task_count = session.scalar(
        select(func.count(TaskRuntimeSummary.id)).where(
            TaskRuntimeSummary.tenant_id == tenant_id,
            TaskRuntimeSummary.task_status.in_({"pending", "running", "wrapping_up"}),
        )
    ) or 0
    failed_action_count = session.scalar(
        select(func.coalesce(func.sum(TaskRuntimeSummary.failed_count), 0)).where(TaskRuntimeSummary.tenant_id == tenant_id)
    ) or 0
    return {
        "tenant_id": tenant_id,
        "open_issue_count": len(open_issues),
        "affected_target_count": len({issue.target_id for issue in open_issues if issue.target_id}),
        "running_task_count": int(running_task_count or 0),
        "failed_action_count": int(failed_action_count or 0),
        "affected_account_count": len(affected_accounts),
        "latest_updated_at": latest_updated_at,
        "stale": bool(latest_updated_at and _now() - latest_updated_at.replace(tzinfo=None) > timedelta(minutes=15)),
    }


def list_operation_issues(
    session: Session,
    tenant_id: int,
    *,
    target_id: int | None = None,
    issue_type: str | None = None,
    severity: str | None = None,
    status: str | None = "open",
    failure_type: str | None = None,
) -> list[OperationIssue]:
    stmt = select(OperationIssue).where(OperationIssue.tenant_id == tenant_id)
    if target_id is not None:
        stmt = stmt.where(OperationIssue.target_id == target_id)
    if issue_type:
        stmt = stmt.where(OperationIssue.issue_type == issue_type)
    if severity:
        stmt = stmt.where(OperationIssue.severity == severity)
    if status:
        stmt = stmt.where(OperationIssue.status == status)
    if failure_type:
        stmt = stmt.where(OperationIssue.failure_type == failure_type)
    return list(session.scalars(stmt.order_by(OperationIssue.status.asc(), OperationIssue.last_seen_at.desc())))


def get_operation_issue_detail(session: Session, tenant_id: int, issue_id: str) -> dict[str, Any]:
    issue = _get_operation_issue(session, tenant_id, issue_id)
    target = session.get(OperationTarget, issue.target_id) if issue.target_id else None
    task = session.get(Task, issue.source_task_id) if issue.source_task_id else None
    task_summary = session.scalar(
        select(TaskRuntimeSummary).where(TaskRuntimeSummary.tenant_id == tenant_id, TaskRuntimeSummary.task_id == issue.source_task_id)
    ) if issue.source_task_id else None
    failed_actions = list(
        session.scalars(
            select(Action)
            .where(Action.tenant_id == tenant_id, Action.task_id == issue.source_task_id, Action.status == "failed")
            .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
            .limit(20)
        )
    ) if issue.source_task_id else []
    account_ids = [int(item) for item in (issue.affected_account_ids or []) if item]
    sources = list(
        session.scalars(
            select(OperationIssueSource)
            .where(OperationIssueSource.tenant_id == tenant_id, OperationIssueSource.issue_id == issue.id)
            .order_by(OperationIssueSource.latest_seen_at.desc())
            .limit(50)
        )
    )
    issue_accounts = list(
        session.scalars(
            select(OperationIssueAccount)
            .where(OperationIssueAccount.tenant_id == tenant_id, OperationIssueAccount.issue_id == issue.id)
            .order_by(OperationIssueAccount.latest_seen_at.desc())
            .limit(50)
        )
    )
    if not account_ids:
        account_ids = [item.account_id for item in issue_accounts]
    accounts = list(session.scalars(select(TgAccount).where(TgAccount.tenant_id == tenant_id, TgAccount.id.in_(account_ids)))) if account_ids else []
    return {
        "issue": issue,
        "target": _target_payload(target),
        "source_task": _task_light_payload(task),
        "related_task_summary": task_summary,
        "sources": sources,
        "issue_accounts": issue_accounts,
        "affected_accounts": [_account_light_payload(account) for account in accounts],
        "recent_failed_actions": [_failed_action_payload(action, task) for action in failed_actions],
    }


def claim_operation_issue(session: Session, tenant_id: int, issue_id: str, actor: str, reason: str = "") -> OperationIssue:
    issue = _get_operation_issue(session, tenant_id, issue_id)
    now_value = _now()
    issue.claimed_by = actor
    issue.claimed_at = now_value
    issue.updated_at = now_value
    issue.summary = {
        **(issue.summary or {}),
        "claimed_by": actor,
        "claim_reason": reason,
        "claimed_at": now_value.isoformat(),
    }
    audit(session, tenant_id=tenant_id, actor=actor, action="认领运营异常", target_type="operation_issue", target_id=issue.id, detail=reason)
    return issue


def acknowledge_operation_issue(session: Session, tenant_id: int, issue_id: str, actor: str, reason: str) -> OperationIssue:
    issue = _get_operation_issue(session, tenant_id, issue_id)
    now_value = _now()
    issue.status = "acknowledged"
    issue.summary = {
        **(issue.summary or {}),
        "acknowledged_by": actor,
        "acknowledge_reason": reason,
        "acknowledged_at": now_value.isoformat(),
    }
    issue.updated_at = now_value
    audit(session, tenant_id=tenant_id, actor=actor, action="确认运营异常", target_type="operation_issue", target_id=issue.id, detail=reason)
    if issue.target_id:
        refresh_target_summary(session, tenant_id, issue.target_id)
    return issue


def ignore_operation_issue(session: Session, tenant_id: int, issue_id: str, actor: str, reason: str) -> OperationIssue:
    issue = _get_operation_issue(session, tenant_id, issue_id)
    now_value = _now()
    issue.status = "ignored"
    issue.resolved_at = now_value
    issue.updated_at = now_value
    issue.summary = {**(issue.summary or {}), "ignore_reason": reason, "ignored_by": actor}
    audit(session, tenant_id=tenant_id, actor=actor, action="忽略运营异常", target_type="operation_issue", target_id=issue.id, detail=reason)
    if issue.target_id:
        refresh_target_summary(session, tenant_id, issue.target_id)
    return issue


def _get_or_create_task_summary(session: Session, tenant_id: int, task_id: str) -> TaskRuntimeSummary:
    summary = session.scalar(select(TaskRuntimeSummary).where(TaskRuntimeSummary.tenant_id == tenant_id, TaskRuntimeSummary.task_id == task_id))
    if summary:
        return summary
    summary = TaskRuntimeSummary(tenant_id=tenant_id, task_id=task_id, updated_at=_now())
    session.add(summary)
    return summary


def _get_or_create_target_summary(session: Session, tenant_id: int, target_id: int) -> TargetRuntimeSummary:
    summary = session.scalar(select(TargetRuntimeSummary).where(TargetRuntimeSummary.tenant_id == tenant_id, TargetRuntimeSummary.target_id == target_id))
    if summary:
        return summary
    summary = TargetRuntimeSummary(tenant_id=tenant_id, target_id=target_id, updated_at=_now())
    session.add(summary)
    return summary


def _get_or_create_account_summary(session: Session, tenant_id: int, account_id: int) -> AccountRuntimeSummary:
    summary = session.scalar(select(AccountRuntimeSummary).where(AccountRuntimeSummary.tenant_id == tenant_id, AccountRuntimeSummary.account_id == account_id))
    if summary:
        return summary
    summary = AccountRuntimeSummary(tenant_id=tenant_id, account_id=account_id, updated_at=_now())
    session.add(summary)
    return summary


def _task_target_id(task: Task) -> int | None:
    config = task.type_config or {}
    for key in ("target_operation_target_id", "target_channel_id", "operation_target_id"):
        value = config.get(key)
        if value:
            return int(value)
    values = config.get("target_operation_target_ids")
    if isinstance(values, list) and values:
        return int(values[0])
    return None


def _task_status_counts(rows: list[TaskRuntimeSummary]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.task_status] = counts.get(row.task_status, 0) + 1
    return counts


def _task_account_ids(task: Task, latest_failure: Action | None) -> set[int]:
    ids = {int(item) for item in (task.account_config or {}).get("account_ids", []) if item}
    if latest_failure and latest_failure.account_id:
        ids.add(int(latest_failure.account_id))
    return ids


def _failure_type(action: Action | None) -> str:
    if not action:
        return ""
    result = action.result or {}
    return _failure_type_code(str(result.get("failure_type") or result.get("error_code") or result.get("error") or "unknown"))


def _failure_reason(action: Action | None) -> str:
    if not action:
        return ""
    result = action.result or {}
    return str(result.get("error_message") or result.get("detail") or result.get("failure_detail") or _failure_type(action))


def _issue_type(action: Action) -> str:
    if action.action_type in {"post_comment", "view_message", "like_message"}:
        return "target_permission"
    if action.action_type in {"send_message"}:
        return "task_execution"
    return "runtime"


def _message_task_target_id(session: Session, task: MessageTask) -> int | None:
    peer_id = task.target_peer_id or ""
    if not peer_id and task.group_id:
        group = session.get(TgGroup, task.group_id)
        peer_id = group.tg_peer_id if group else ""
    if not peer_id:
        return None
    return session.scalar(
        select(OperationTarget.id).where(
            OperationTarget.tenant_id == task.tenant_id,
            OperationTarget.tg_peer_id == peer_id,
        )
    )


def _message_task_suggested_action(failure_type: str) -> str:
    if failure_type == FailureType.GROUP_PERMISSION_DENIED.value:
        return "打开运营目标处理准入或发送权限，再回到消息发送重试"
    if failure_type == FailureType.ACCOUNT_UNAVAILABLE.value:
        return "检查账号在线、代理和风控状态，再回到消息发送重试"
    return _suggested_action(failure_type)


def _suggested_action(failure_type: str) -> str:
    mapping = {
        "COMMENT_UNAVAILABLE": "检查频道讨论组绑定和账号评论权限",
        "ACCOUNT_UNAVAILABLE": "检查账号在线、代理和风控状态",
        "CONTENT_REJECTED": "检查规则中心拦截和素材策略",
        "FLOOD_WAIT": "等待 Telegram FloodWait 解除后再恢复该账号",
        "SLOWMODE": "等待目标慢速模式窗口结束后再恢复发送",
    }
    return mapping.get(failure_type, "查看任务详情和账号状态后处理")


def _failure_type_code(failure_type: str) -> str:
    return FAILURE_TYPE_CODES.get(failure_type, failure_type)


def _handling_mode(failure_type: str) -> str:
    if failure_type in {"COMMENT_UNAVAILABLE", "PEER_INVALID", "CONTENT_REJECTED"}:
        return "drawer"
    if failure_type in {"ACCOUNT_UNAVAILABLE", "FLOOD_WAIT", "SLOWMODE"}:
        return "drawer"
    return "deep_link"


def _issue_return_to(issue: OperationIssue, *, source_task_id: str, representative_action_id: str) -> dict[str, Any]:
    return {
        "page": "overview",
        "source_issue_id": issue.id,
        "target_id": issue.target_id,
        "task_id": source_task_id,
        "action_id": representative_action_id,
        "default_tab": "issues",
        "filters": {
            "target_id": issue.target_id,
            "issue_type": issue.issue_type,
            "failure_type": issue.failure_type,
            "status": issue.status,
        },
    }


def _issue_has_source(session: Session, tenant_id: int, issue_id: str, source_type: str, source_id: str) -> bool:
    if not source_id:
        return False
    return bool(
        session.scalar(
            select(OperationIssueSource.id)
            .where(
                OperationIssueSource.tenant_id == tenant_id,
                OperationIssueSource.issue_id == issue_id,
                OperationIssueSource.source_type == source_type,
                OperationIssueSource.source_id == source_id,
            )
            .limit(1)
        )
    )


def _issue_source_count(session: Session, tenant_id: int, issue_id: str, source_type: str) -> int:
    return int(
        session.scalar(
            select(func.count(OperationIssueSource.id)).where(
                OperationIssueSource.tenant_id == tenant_id,
                OperationIssueSource.issue_id == issue_id,
                OperationIssueSource.source_type == source_type,
            )
        )
        or 0
    )


def _issue_account_count(session: Session, tenant_id: int, issue_id: str) -> int:
    return int(
        session.scalar(
            select(func.count(OperationIssueAccount.id)).where(
                OperationIssueAccount.tenant_id == tenant_id,
                OperationIssueAccount.issue_id == issue_id,
            )
        )
        or 0
    )


def _issue_has_failed_message_task_source(session: Session, tenant_id: int, issue_id: str) -> bool:
    source_ids = [
        int(source_id)
        for source_id in session.scalars(
            select(OperationIssueSource.source_id).where(
                OperationIssueSource.tenant_id == tenant_id,
                OperationIssueSource.issue_id == issue_id,
                OperationIssueSource.source_type == "message_task",
            )
        )
        if str(source_id).isdigit()
    ]
    if not source_ids:
        return False
    return bool(
        session.scalar(
            select(MessageTask.id)
            .where(
                MessageTask.tenant_id == tenant_id,
                MessageTask.id.in_(source_ids),
                MessageTask.status == "失败",
            )
            .limit(1)
        )
    )


def _upsert_issue_source(
    session: Session,
    tenant_id: int,
    issue_id: str,
    source_type: str,
    source_id: str,
    failure_type: str,
    latest_seen_at: datetime,
    summary: dict[str, Any],
) -> None:
    if not source_id:
        return
    source = session.scalar(
        select(OperationIssueSource).where(
            OperationIssueSource.tenant_id == tenant_id,
            OperationIssueSource.issue_id == issue_id,
            OperationIssueSource.source_type == source_type,
            OperationIssueSource.source_id == source_id,
        )
    )
    if not source:
        source = OperationIssueSource(tenant_id=tenant_id, issue_id=issue_id, source_type=source_type, source_id=source_id)
        session.add(source)
    source.failure_type = failure_type
    source.latest_seen_at = latest_seen_at
    source.summary = summary


def _upsert_issue_account(
    session: Session,
    tenant_id: int,
    issue_id: str,
    account_id: int,
    impact_type: str,
    latest_seen_at: datetime,
    summary: dict[str, Any],
) -> None:
    account = session.scalar(
        select(OperationIssueAccount).where(
            OperationIssueAccount.tenant_id == tenant_id,
            OperationIssueAccount.issue_id == issue_id,
            OperationIssueAccount.account_id == account_id,
            OperationIssueAccount.impact_type == impact_type,
        )
    )
    if not account:
        account = OperationIssueAccount(tenant_id=tenant_id, issue_id=issue_id, account_id=account_id, impact_type=impact_type)
        session.add(account)
    account.latest_seen_at = latest_seen_at
    account.summary = summary


def _account_rate_limit_signal(session: Session, tenant_id: int, account_id: int, recent_cutoff: datetime) -> tuple[datetime | None, str, int]:
    actions = list(
        session.scalars(
            select(Action)
            .where(Action.tenant_id == tenant_id, Action.account_id == account_id, Action.created_at >= recent_cutoff)
            .order_by(Action.created_at.desc())
            .limit(100)
        )
    )
    retry_candidates: list[datetime] = []
    reason = ""
    count = 0
    for action in actions:
        result = action.result or {}
        code = str(result.get("error_code") or result.get("failure_type") or "")
        detail = str(result.get("error_message") or result.get("detail") or "")
        if not _is_rate_limit_failure(code, detail):
            continue
        count += 1
        reason = code or reason or "rate_limit"
        retry_at = _parse_datetime(result.get("next_retry_at"))
        retry_after = _safe_int(result.get("retry_after_seconds"))
        if retry_at is None and retry_after > 0 and action.executed_at:
            retry_at = _naive_datetime(action.executed_at) + timedelta(seconds=retry_after)
        if retry_at is None and action.status == "pending" and action.scheduled_at:
            retry_at = _naive_datetime(action.scheduled_at)
        if retry_at:
            retry_candidates.append(retry_at)
    future_candidates = [item for item in retry_candidates if item > _now()]
    return (max(future_candidates) if future_candidates else None, reason, count)


def _account_security_signal(session: Session, tenant_id: int, account_id: int) -> tuple[bool, str, dict[str, Any]]:
    snapshot = session.scalar(
        select(TgAccountSecuritySnapshot).where(
            TgAccountSecuritySnapshot.tenant_id == tenant_id,
            TgAccountSecuritySnapshot.account_id == account_id,
        )
    )
    if not snapshot:
        return False, "", {}
    blocked_reason = _security_blocked_reason(snapshot)
    risk_reason = "；".join(_security_score_reasons(snapshot))
    trend: dict[str, Any] = {
        "trusted_session_status": snapshot.trusted_session_status or "unknown",
        "two_fa_status": snapshot.two_fa_status or "unknown",
        "external_authorization_count": int(snapshot.external_authorization_count or 0),
        "security_profile_status": snapshot.profile_status or "unknown",
        "security_blocked": bool(blocked_reason),
        "security_risk_reason": risk_reason,
    }
    if snapshot.last_error:
        trend["security_last_error"] = snapshot.last_error
    if snapshot.trace_id:
        trend["security_trace_id"] = snapshot.trace_id
    return bool(blocked_reason), blocked_reason or risk_reason, trend


def _account_security_next_retry_at(session: Session, tenant_id: int, account_id: int) -> datetime | None:
    rows = session.scalars(
        select(TgAccountSecurityBatchItem.next_retry_at)
        .where(
            TgAccountSecurityBatchItem.tenant_id == tenant_id,
            TgAccountSecurityBatchItem.account_id == account_id,
            TgAccountSecurityBatchItem.status.in_(SECURITY_RETRY_STATUSES),
            TgAccountSecurityBatchItem.next_retry_at.is_not(None),
        )
        .order_by(TgAccountSecurityBatchItem.next_retry_at.desc())
        .limit(20)
    )
    future_values = [_naive_datetime(value) for value in rows if value and _naive_datetime(value) > _now()]
    return max(future_values) if future_values else None


def _account_recent_risk_signal(session: Session, tenant_id: int, account_id: int, recent_cutoff: datetime) -> dict[str, Any]:
    actions = list(
        session.scalars(
            select(Action)
            .where(
                Action.tenant_id == tenant_id,
                Action.account_id == account_id,
                Action.created_at >= recent_cutoff,
                Action.status.in_(RISK_SIGNAL_STATUSES),
            )
            .order_by(Action.created_at.desc())
            .limit(50)
        )
    )
    for action in actions:
        result = action.result or {}
        reason = _risk_result_reason(result)
        decision = str(result.get("decision") or "")
        risk_level = str(result.get("risk_level") or "")
        if not (decision or risk_level or reason):
            continue
        trend: dict[str, Any] = {
            "recent_risk_action_id": action.id,
            "recent_risk_status": action.status,
            "recent_risk_reason": reason or _failure_reason(action),
        }
        if decision:
            trend["recent_risk_decision"] = decision
        if risk_level:
            trend["recent_risk_level"] = risk_level
        suggested_actions = _result_list(result, "suggested_actions")
        if suggested_actions:
            trend["recent_risk_suggested_actions"] = suggested_actions[:3]
        return trend
    return {}


def _is_rate_limit_failure(code: str, detail: str) -> bool:
    text = f"{code} {detail}".lower()
    return any(marker in text for marker in ["flood", "slowmode", "慢速", "限流"])


def _security_blocked_reason(snapshot: TgAccountSecuritySnapshot | None) -> str:
    if snapshot is None:
        return ""
    if snapshot.trusted_session_status == "missing":
        return "平台可信设备无法确认"
    if snapshot.two_fa_status == "failed":
        return "二步验证设置失败"
    if snapshot.two_fa_status in {"email_confirmation_required", "pending_email_confirmation"}:
        return "二步验证待邮箱确认"
    return ""


def _security_score_reasons(snapshot: TgAccountSecuritySnapshot | None) -> list[str]:
    if snapshot is None:
        return []
    reasons: list[str] = []
    if snapshot.trusted_session_status in {"missing", "unknown", "failed"}:
        reasons.append(f"平台可信设备：{snapshot.trusted_session_status}")
    if snapshot.external_authorization_count > 0:
        reasons.append(f"存在 {snapshot.external_authorization_count} 个外部登录设备")
    if snapshot.two_fa_status in {"missing", "unknown", "failed", "email_confirmation_required", "pending_email_confirmation"}:
        reasons.append(f"二步验证：{snapshot.two_fa_status}")
    if snapshot.profile_status in {"unknown", "incomplete", "update_failed"}:
        reasons.append(f"资料状态：{snapshot.profile_status}")
    return reasons


def _risk_result_reason(result: dict[str, Any]) -> str:
    for key in ("decision_reasons", "blockers", "risk_hits", "proxy_warnings", "target_warnings", "content_warnings"):
        values = _result_list(result, key)
        if values:
            return "；".join(values[:3])
    for key in ("failure_type", "error_code", "error", "error_message", "detail", "failure_detail", "reason"):
        value = result.get(key)
        if value:
            return str(value)
    return ""


def _result_list(result: dict[str, Any], key: str) -> list[str]:
    value = result.get(key)
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        reason = value.get("reason_code") or value.get("reason") or value.get("detail") or value.get("title")
        return [str(reason)] if reason else []
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            if isinstance(item, str):
                items.append(item)
            elif isinstance(item, dict):
                reason = item.get("reason_code") or item.get("reason") or item.get("detail") or item.get("title")
                if reason:
                    items.append(str(reason))
            elif item:
                items.append(str(item))
        return items
    return [str(value)]


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _naive_datetime(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _naive_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _later_retry_at(left: datetime | None, right: datetime | None) -> datetime | None:
    values = [value for value in (left, right) if value]
    return max(values) if values else None


def _account_unavailable_reason(account: TgAccount | None) -> str:
    if not account:
        return "account_not_found"
    if account.deleted_at:
        return "account_deleted"
    if not account.session_ciphertext:
        return "session_missing"
    if account.status != AccountStatus.ACTIVE.value:
        return account.status
    return ""


def _account_proxy_unavailable_reason(account: TgAccount | None) -> str:
    if not account or not account.proxy_id:
        return ""
    if account.proxy_status in {"healthy", "健康"}:
        return ""
    return f"proxy_unavailable:{account.proxy_status or 'unknown'}"


def _get_operation_issue(session: Session, tenant_id: int, issue_id: str) -> OperationIssue:
    issue = session.get(OperationIssue, issue_id)
    if not issue or issue.tenant_id != tenant_id:
        raise ValueError("operation issue not found")
    return issue


def _target_payload(target: OperationTarget | None) -> dict[str, Any] | None:
    if not target:
        return None
    return {
        "id": target.id,
        "target_type": target.target_type,
        "title": target.title,
        "username": target.username,
        "member_count": target.member_count,
        "auth_status": target.auth_status,
        "can_send": target.can_send,
        "last_sync_at": target.last_sync_at,
        "updated_at": target.updated_at,
    }


def _task_light_payload(task: Task | None) -> dict[str, Any] | None:
    if not task:
        return None
    return {
        "id": task.id,
        "name": task.name,
        "type": task.type,
        "status": task.status,
        "priority": task.priority,
        "last_error": task.last_error,
        "updated_at": task.updated_at,
    }


def _account_light_payload(account: TgAccount) -> dict[str, Any]:
    return {
        "id": account.id,
        "display_name": account.display_name,
        "username": account.username,
        "status": account.status,
        "health_score": account.health_score,
    }


def _failed_action_payload(action: Action, task: Task | None) -> dict[str, Any]:
    return {
        "id": action.id,
        "task_id": action.task_id,
        "task_name": task.name if task else "",
        "task_type": action.task_type,
        "action_type": action.action_type,
        "account_id": action.account_id,
        "status": action.status,
        "failure_type": _failure_type(action),
        "failure_reason": _failure_reason(action),
        "scheduled_at": action.scheduled_at,
        "executed_at": action.executed_at,
        "retry_count": action.retry_count,
        "result": action.result or {},
    }


__all__ = [
    "acknowledge_operation_issue",
    "claim_operation_issue",
    "get_operation_issue_detail",
    "rebuild_runtime_summaries",
    "get_account_runtime_summary",
    "ignore_operation_issue",
    "list_operation_issues",
    "list_account_runtime_summaries",
    "list_target_runtime_summaries",
    "operation_center_overview",
    "refresh_account_summary",
    "refresh_target_summary",
    "refresh_task_summary",
    "resolve_operation_issue",
    "resolve_message_task_issues_if_recovered",
    "rollup_message_task_failure",
    "upsert_operation_issue",
]
