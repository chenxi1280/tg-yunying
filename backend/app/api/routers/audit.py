"""Audit log routes."""
from __future__ import annotations


from collections.abc import Sequence
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, resolve_tenant_id
from app.database import get_session
from app.api.response_permissions import audit_log_out_for_user
from app.schemas import AuditLogOut
from app.services._common import audit as write_audit
from app.services import audit_logs_csv, filter_audit_logs

router = APIRouter()


@router.get("/api/audit-logs", response_model=list[AuditLogOut])
def list_audit_logs(
    tenant_id: int | None = None,
    actor: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    keyword: str | None = None,
    account_id: str | None = None,
    operation_target_id: str | None = None,
    task_id: str | None = None,
    status: str | None = None,
    start_at: str | None = None,
    end_at: str | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[dict]:
    tenant_id = resolve_tenant_id(current_user, tenant_id)
    try:
        parsed_start = datetime.fromisoformat(start_at) if start_at else None
        parsed_end = datetime.fromisoformat(end_at) if end_at else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid datetime filter") from exc
    logs = filter_audit_logs(
        session,
        tenant_id,
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        keyword=keyword,
        account_id=account_id,
        operation_target_id=operation_target_id,
        task_id=task_id,
        status=status,
        start_at=parsed_start,
        end_at=parsed_end,
    )
    return [audit_log_out_for_user(log, current_user) for log in logs]


@router.get("/api/audit-logs/export")
def export_audit_logs(
    tenant_id: int | None = None,
    actor: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    keyword: str | None = None,
    account_id: str | None = None,
    operation_target_id: str | None = None,
    task_id: str | None = None,
    status: str | None = None,
    start_at: str | None = None,
    end_at: str | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Response:
    tenant_id = resolve_tenant_id(current_user, tenant_id)
    try:
        parsed_start = datetime.fromisoformat(start_at) if start_at else None
        parsed_end = datetime.fromisoformat(end_at) if end_at else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid datetime filter") from exc
    logs = filter_audit_logs(
        session,
        tenant_id,
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        keyword=keyword,
        account_id=account_id,
        operation_target_id=operation_target_id,
        task_id=task_id,
        status=status,
        start_at=parsed_start,
        end_at=parsed_end,
        limit=5000,
    )
    write_audit(
        session,
        tenant_id=tenant_id,
        actor=current_user.name,
        action="导出审计记录",
        target_type="audit_log",
        target_id="export",
        detail=f"count={len(logs)}",
    )
    session.commit()
    payload = [audit_log_out_for_user(log, current_user) for log in logs]
    return Response(
        content=audit_logs_csv(payload),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit-logs.csv"'},
    )
