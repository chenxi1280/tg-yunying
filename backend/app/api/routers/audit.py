"""Audit log routes."""
from __future__ import annotations


from collections.abc import Sequence
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, resolve_tenant_id
from app.database import get_session
from app.models import AuditLog
from app.schemas import AuditLogOut
from app.services import filter_audit_logs

router = APIRouter()


@router.get("/api/audit-logs", response_model=list[AuditLogOut])
def list_audit_logs(
    tenant_id: int | None = None,
    actor: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    start_at: str | None = None,
    end_at: str | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[AuditLog]:
    tenant_id = resolve_tenant_id(current_user, tenant_id)
    try:
        parsed_start = datetime.fromisoformat(start_at) if start_at else None
        parsed_end = datetime.fromisoformat(end_at) if end_at else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid datetime filter") from exc
    return filter_audit_logs(session, tenant_id, actor=actor, action=action, target_type=target_type, start_at=parsed_start, end_at=parsed_end)
