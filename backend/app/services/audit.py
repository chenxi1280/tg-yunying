from __future__ import annotations

import csv
from io import StringIO
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import AuditLog

from ._common import require_tenant


def filter_audit_logs(
    session: Session,
    tenant_id: int,
    *,
    actor: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    keyword: str | None = None,
    account_id: str | None = None,
    operation_target_id: str | None = None,
    task_id: str | None = None,
    status: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    limit: int = 100,
) -> list[AuditLog]:
    require_tenant(session, tenant_id)
    stmt = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
    if actor:
        stmt = stmt.where(AuditLog.actor.like(f"%{actor}%"))
    if action:
        stmt = stmt.where(AuditLog.action.like(f"%{action}%"))
    if target_type:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if target_id:
        stmt = stmt.where(AuditLog.target_id == target_id)
    if account_id:
        stmt = stmt.where(AuditLog.target_type.in_(["tg_account", "account_sync_record", "manual_operation"]), AuditLog.target_id == account_id)
    if operation_target_id:
        stmt = stmt.where(AuditLog.target_type.in_(["operation_target", "tg_group"]), AuditLog.target_id == operation_target_id)
    if task_id:
        stmt = stmt.where(
            or_(
                AuditLog.target_id == task_id,
                AuditLog.detail.like(f"%{task_id}%"),
            )
        )
    if keyword:
        pattern = f"%{keyword}%"
        stmt = stmt.where(or_(AuditLog.action.like(pattern), AuditLog.actor.like(pattern), AuditLog.target_type.like(pattern), AuditLog.target_id.like(pattern), AuditLog.detail.like(pattern)))
    if status:
        status_map = {
            "success": ["成功", "完成", "通过", "创建", "新增", "更新", "启动", "发布", "同步", "导出", "发送"],
            "failed": ["失败", "异常", "错误"],
            "skipped": ["跳过", "忽略", "取消", "停止", "删除"],
        }
        words = status_map.get(status, [status])
        stmt = stmt.where(or_(*[AuditLog.action.like(f"%{word}%") for word in words], *[AuditLog.detail.like(f"%{word}%") for word in words]))
    if start_at:
        stmt = stmt.where(AuditLog.created_at >= start_at)
    if end_at:
        stmt = stmt.where(AuditLog.created_at <= end_at)
    return list(session.scalars(stmt.order_by(AuditLog.id.desc()).limit(limit)))


def audit_logs_csv(logs: list[AuditLog]) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "tenant_id", "actor", "action", "target_type", "target_id", "detail", "ip_address", "created_at"])
    for item in logs:
        writer.writerow([
            item.id,
            item.tenant_id,
            item.actor,
            item.action,
            item.target_type,
            item.target_id,
            item.detail,
            item.ip_address,
            item.created_at.isoformat() if item.created_at else "",
        ])
    return output.getvalue()


__all__ = ["audit_logs_csv", "filter_audit_logs"]
