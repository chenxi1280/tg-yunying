from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    Action,
    AiDraft,
    AuditLog,
    AiUsageLedger,
    GroupAuthStatus,
    MessageTask,
    OperationTarget,
    RuleSet,
    Task,
    TaskStatus,
    TgAccount,
    TgGroup,
    VerificationTask,
)
from app.services._common import _now
from app.services.runtime_summary import operation_center_overview
from app.timezone import as_beijing, beijing_day_bounds

UNRESOLVED_FAILURE_STATUSES = {"failed", "retryable_failed", "unknown_after_send"}


def build_overview(session: Session, tenant_id: int | None = None) -> dict:
    account_stmt = select(func.count(TgAccount.id)).where(TgAccount.deleted_at.is_(None))
    group_stmt = select(func.count(TgGroup.id))
    target_stmt = select(func.count(OperationTarget.id))
    operation_task_stmt = select(func.count(Task.id)).where(Task.deleted_at.is_(None))
    task_base = []
    task_filters = [Task.deleted_at.is_(None)]
    action_filters = []
    if tenant_id is not None:
        account_stmt = account_stmt.where(TgAccount.tenant_id == tenant_id)
        group_stmt = group_stmt.where(TgGroup.tenant_id == tenant_id)
        target_stmt = target_stmt.where(OperationTarget.tenant_id == tenant_id)
        operation_task_stmt = operation_task_stmt.where(Task.tenant_id == tenant_id)
        task_filters.append(Task.tenant_id == tenant_id)
        action_filters.append(Action.tenant_id == tenant_id)
        task_base.append(MessageTask.tenant_id == tenant_id)

    total_accounts = session.scalar(account_stmt) or 0
    total_groups = session.scalar(group_stmt) or 0
    total_targets = session.scalar(target_stmt) or 0
    total_operation_tasks = session.scalar(operation_task_stmt) or 0
    running_tasks = session.scalar(select(func.count(Task.id)).where(*task_filters, Task.status == "running")) or 0
    paused_tasks = session.scalar(select(func.count(Task.id)).where(*task_filters, Task.status == "paused")) or 0
    failed_tasks = session.scalar(select(func.count(Task.id)).where(*task_filters, Task.status == "failed")) or 0
    pending_tasks = session.scalar(select(func.count(Task.id)).where(*task_filters, Task.status == "pending")) or 0
    queued = session.scalar(select(func.count(MessageTask.id)).where(*task_base, MessageTask.status == TaskStatus.QUEUED.value)) or 0
    sent = session.scalar(select(func.count(MessageTask.id)).where(*task_base, MessageTask.status == TaskStatus.SENT.value)) or 0
    failed = session.scalar(select(func.count(MessageTask.id)).where(*task_base, MessageTask.status == TaskStatus.FAILED.value)) or 0
    total_tasks = sent + failed + queued
    success_rate = round(sent / max(sent + failed, 1) * 100, 1)
    account_filters = [TgAccount.deleted_at.is_(None), *([TgAccount.tenant_id == tenant_id] if tenant_id is not None else [])]
    avg_health = session.scalar(select(func.coalesce(func.avg(TgAccount.health_score), 0)).where(*account_filters)) or 0
    draft_filters = [AiDraft.tenant_id == tenant_id] if tenant_id is not None else []
    review_total = session.scalar(select(func.count(AiDraft.id)).where(*draft_filters)) or 0
    review_done = session.scalar(select(func.count(AiDraft.id)).where(*draft_filters, AiDraft.status == TaskStatus.APPROVED.value)) or 0
    review_completion = round(review_done / max(review_total, 1) * 100, 1)
    usage_filters = [AiUsageLedger.tenant_id == tenant_id] if tenant_id is not None else []
    total_usage_tokens = session.scalar(select(func.coalesce(func.sum(AiUsageLedger.total_tokens), 0)).where(*usage_filters)) or 0
    total_usage_cost = session.scalar(select(func.coalesce(func.sum(AiUsageLedger.total_cost), 0)).where(*usage_filters)) or 0
    verification_filters = [VerificationTask.tenant_id == tenant_id] if tenant_id is not None else []
    pending_verifications = session.scalar(select(func.count(VerificationTask.id)).where(*verification_filters, VerificationTask.status == "待处理")) or 0
    limited_accounts = session.scalar(select(func.count(TgAccount.id)).where(*account_filters, TgAccount.status.in_([AccountStatus.LIMITED.value, AccountStatus.NEED_RELOGIN.value]))) or 0
    readonly_groups = session.scalar(select(func.count(TgGroup.id)).where(*([TgGroup.tenant_id == tenant_id] if tenant_id is not None else []), TgGroup.auth_status != GroupAuthStatus.AUTHORIZED.value)) or 0
    listener_error_groups = session.scalar(select(func.count(TgGroup.id)).where(*([TgGroup.tenant_id == tenant_id] if tenant_id is not None else []), TgGroup.listener_last_error != "")) or 0
    failed_actions = session.scalar(select(func.count(Action.id)).where(*action_filters, Action.status.in_(UNRESOLVED_FAILURE_STATUSES))) or 0
    pending_actions = session.scalar(select(func.count(Action.id)).where(*action_filters, Action.status.in_(["pending", "executing"]))) or 0
    rule_filters = [RuleSet.tenant_id == tenant_id] if tenant_id is not None else []
    active_rules = session.scalar(select(func.count(RuleSet.id)).where(*rule_filters, RuleSet.status == "active")) or 0
    risks: list[dict[str, str]] = []
    if pending_verifications:
        risks.append({"level": "中", "title": f"{pending_verifications} 个验证辅助待处理", "detail": "存在群验证、关注或按钮确认任务尚未完成。"})
    if limited_accounts:
        risks.append({"level": "中", "title": f"{limited_accounts} 个账号需关注", "detail": "包含受限或需重新登录的账号，建议优先做健康检查。"})
    if readonly_groups:
        risks.append({"level": "低", "title": f"{readonly_groups} 个群暂不可运营", "detail": "这些群当前为未授权、只读归档或禁止操作状态。"})
    if failed_tasks:
        risks.append({"level": "高", "title": f"{failed_tasks} 个任务失败", "detail": "建议进入任务中心查看失败原因并重试或重置。"})
    if listener_error_groups:
        risks.append({"level": "高", "title": f"{listener_error_groups} 个监听对象异常", "detail": "建议进入监听中心查看备用账号和最近事件。"})
    if failed_actions:
        risks.append({"level": "中", "title": f"{failed_actions} 个执行项失败或结果未知", "detail": "需要按账号、目标、内容规则或 TG API 返回逐项排查。"})

    return {
        "totals": {
            "accounts": total_accounts,
            "groups": total_groups,
            "targets": total_targets,
            "tasks": total_operation_tasks,
            "campaigns": total_operation_tasks,
            "message_tasks": total_tasks,
            "ai_tokens": int(total_usage_tokens),
            "rules": active_rules,
        },
        "rates": {
            "send_success": success_rate,
            "account_health": round(float(avg_health), 1),
            "review_completion": review_completion,
            "ai_cost": float(total_usage_cost),
        },
        "queue": {
            "queued": queued,
            "sent": sent,
            "failed": failed,
            "running_tasks": running_tasks,
            "pending_tasks": pending_tasks,
            "paused_tasks": paused_tasks,
            "failed_tasks": failed_tasks,
            "pending_actions": pending_actions,
            "failed_actions": failed_actions,
            "listener_errors": listener_error_groups,
        },
        "risks": risks,
        "activity_24h": _hourly_activity_24h(session, tenant_id),
        "operation_center": operation_center_overview(session, tenant_id) if tenant_id is not None else None,
    }


def _hourly_activity_24h(session: Session, tenant_id: int | None = None) -> list[dict[str, int | float | str]]:
    now_value = _now()
    current_hour = now_value.replace(minute=0, second=0, microsecond=0)
    start_hour = current_hour - timedelta(hours=23)
    end_hour = current_hour + timedelta(hours=1)
    buckets = _empty_hourly_buckets(start_hour)

    filters = [
        Action.executed_at.is_not(None),
        Action.executed_at >= start_hour,
        Action.executed_at < end_hour,
        Action.action_type.in_(["send_message", "like_message", "post_comment"]),
        Action.status.in_(["success", *UNRESOLVED_FAILURE_STATUSES]),
    ]
    if tenant_id is not None:
        filters.append(Action.tenant_id == tenant_id)

    rows = session.execute(
        select(Action.executed_at, Action.action_type, Action.status)
        .where(*filters)
        .order_by(Action.executed_at.asc())
    ).all()
    for executed_at, action_type, status in rows:
        _touch_hourly_bucket(buckets, executed_at=executed_at, action_type=action_type, status=status)

    for bucket in buckets.values():
        total = int(bucket["total"])
        bucket["success_rate"] = round(int(bucket["success"]) * 100 / total, 1) if total else 0.0
        bucket["failure_rate"] = round(int(bucket["failed"]) * 100 / total, 1) if total else 0.0
    return list(buckets.values())


def _empty_hourly_buckets(start_hour: datetime) -> dict[datetime, dict[str, int | float | str]]:
    buckets: dict[datetime, dict[str, int | float | str]] = {}
    for index in range(24):
        hour = start_hour + timedelta(hours=index)
        buckets[hour] = {
            "hour": hour.strftime("%H:00"),
            "sent_messages": 0,
            "likes": 0,
            "comments": 0,
            "success": 0,
            "failed": 0,
            "total": 0,
            "success_rate": 0.0,
            "failure_rate": 0.0,
        }
    return buckets


def _touch_hourly_bucket(
    buckets: dict[datetime, dict[str, int | float | str]],
    *,
    executed_at: datetime | None,
    action_type: str,
    status: str,
) -> None:
    executed_at = as_beijing(executed_at)
    if executed_at is None:
        return
    bucket = buckets.get(executed_at.replace(minute=0, second=0, microsecond=0))
    if bucket is None:
        return
    bucket["total"] = int(bucket["total"]) + 1
    if status == "success":
        _touch_hourly_success(bucket, action_type)
    elif status in UNRESOLVED_FAILURE_STATUSES:
        bucket["failed"] = int(bucket["failed"]) + 1


def _touch_hourly_success(bucket: dict[str, int | float | str], action_type: str) -> None:
    bucket["success"] = int(bucket["success"]) + 1
    if action_type == "send_message":
        bucket["sent_messages"] = int(bucket["sent_messages"]) + 1
    elif action_type == "like_message":
        bucket["likes"] = int(bucket["likes"]) + 1
    elif action_type == "post_comment":
        bucket["comments"] = int(bucket["comments"]) + 1


def build_report(session: Session, tenant_id: int | None = None) -> dict:
    overview = build_overview(session, tenant_id)
    account_filters = [TgAccount.deleted_at.is_(None), *([TgAccount.tenant_id == tenant_id] if tenant_id is not None else [])]
    group_filters = [TgGroup.tenant_id == tenant_id] if tenant_id is not None else []
    task_filters = [MessageTask.tenant_id == tenant_id] if tenant_id is not None else []
    day_start, day_end = beijing_day_bounds()
    daily_messages = session.scalar(
        select(func.count(MessageTask.id)).where(
            *task_filters,
            MessageTask.status == TaskStatus.SENT.value,
            MessageTask.sent_at.is_not(None),
            MessageTask.sent_at >= day_start,
            MessageTask.sent_at < day_end,
        )
    ) or 0
    avg_delay = session.scalar(select(func.coalesce(func.avg(MessageTask.planned_delay_seconds), 0)).where(*task_filters)) or 0
    return {
        "accounts": {
            "total": overview["totals"]["accounts"],
            "active": session.scalar(select(func.count(TgAccount.id)).where(*account_filters, TgAccount.status == AccountStatus.ACTIVE.value)) or 0,
            "avg_health_score": overview["rates"]["account_health"],
        },
        "groups": {
            "total": overview["totals"]["groups"],
            "authorized": session.scalar(select(func.count(TgGroup.id)).where(*group_filters, TgGroup.auth_status == GroupAuthStatus.AUTHORIZED.value)) or 0,
            "daily_messages": daily_messages,
        },
        "tasks": {
            "total": overview["totals"]["message_tasks"],
            "queued": overview["queue"]["queued"],
            "sent": overview["queue"]["sent"],
            "failed": overview["queue"]["failed"],
            "avg_delay_seconds": int(avg_delay),
        },
        "tenant": {
            "message_consumption": overview["totals"]["message_tasks"],
            "ai_tokens": overview["totals"]["ai_tokens"],
            "ai_cost": overview["rates"]["ai_cost"],
            "risk_events": len(overview["risks"]),
            "audit_events": session.scalar(
                select(func.count(AuditLog.id)).where(AuditLog.tenant_id == tenant_id)
                if tenant_id is not None
                else select(func.count(AuditLog.id))
            )
            or 0,
        },
    }


__all__ = ["build_overview", "build_report"]
