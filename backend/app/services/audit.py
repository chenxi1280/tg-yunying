from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
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
    if start_at:
        stmt = stmt.where(AuditLog.created_at >= start_at)
    if end_at:
        stmt = stmt.where(AuditLog.created_at <= end_at)
    return list(session.scalars(stmt.order_by(AuditLog.id.desc()).limit(limit)))


__all__ = ["filter_audit_logs"]
