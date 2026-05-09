from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    AiDraft,
    AuditLog,
    AiUsageLedger,
    Campaign,
    GroupAuthStatus,
    MessageTask,
    TaskStatus,
    TgAccount,
    TgGroup,
    VerificationTask,
)


def build_overview(session: Session, tenant_id: int | None = None) -> dict:
    account_stmt = select(func.count(TgAccount.id)).where(TgAccount.deleted_at.is_(None))
    group_stmt = select(func.count(TgGroup.id))
    campaign_stmt = select(func.count(Campaign.id))
    task_base = []
    if tenant_id is not None:
        account_stmt = account_stmt.where(TgAccount.tenant_id == tenant_id)
        group_stmt = group_stmt.where(TgGroup.tenant_id == tenant_id)
        campaign_stmt = campaign_stmt.where(Campaign.tenant_id == tenant_id)
        task_base.append(MessageTask.tenant_id == tenant_id)

    total_accounts = session.scalar(account_stmt) or 0
    total_groups = session.scalar(group_stmt) or 0
    total_campaigns = session.scalar(campaign_stmt) or 0
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
    risks: list[dict[str, str]] = []
    if pending_verifications:
        risks.append({"level": "中", "title": f"{pending_verifications} 个验证辅助待处理", "detail": "存在群验证、关注或按钮确认任务尚未完成。"})
    if limited_accounts:
        risks.append({"level": "中", "title": f"{limited_accounts} 个账号需关注", "detail": "包含受限或需重新登录的账号，建议优先做健康检查。"})
    if readonly_groups:
        risks.append({"level": "低", "title": f"{readonly_groups} 个群暂不可运营", "detail": "这些群当前为未授权、只读归档或禁止操作状态。"})

    return {
        "totals": {
            "accounts": total_accounts,
            "groups": total_groups,
            "campaigns": total_campaigns,
            "message_tasks": total_tasks,
            "ai_tokens": int(total_usage_tokens),
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
        },
        "risks": risks,
    }


def build_report(session: Session, tenant_id: int | None = None) -> dict:
    overview = build_overview(session, tenant_id)
    account_filters = [TgAccount.deleted_at.is_(None), *([TgAccount.tenant_id == tenant_id] if tenant_id is not None else [])]
    group_filters = [TgGroup.tenant_id == tenant_id] if tenant_id is not None else []
    task_filters = [MessageTask.tenant_id == tenant_id] if tenant_id is not None else []
    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    day_end = (datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).replace(tzinfo=None)
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
