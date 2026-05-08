"""System routes: health, runtime config, overview, tenants, worker."""
from __future__ import annotations


from collections.abc import Sequence

from fastapi import APIRouter, Depends
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, require_core_feature_access, resolve_tenant_id
from app.config import get_settings
from app.database import get_session
from app.common.http import forbidden
from app.models import Tenant
from app.schemas import (
    OverviewOut,
    ReportOut,
    RuntimeConfigOut,
    TenantCreate,
    TenantNotificationSettingsOut,
    TenantNotificationSettingsUpdate,
    TenantOut,
    TenantUpdate,
)
from app.services import (
    build_overview,
    build_report,
    create_tenant,
    get_runtime_config,
    notification_settings_payload,
    update_tenant,
    update_tenant_notification_settings,
)
from app.worker import drain_once

router = APIRouter()


@router.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/config/runtime", response_model=RuntimeConfigOut)
def runtime_config(session: Session = Depends(get_session)) -> dict:
    return get_runtime_config(session)


@router.get("/api/overview", response_model=OverviewOut)
def overview(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    return build_overview(session, resolve_tenant_id(current_user, tenant_id))


@router.get("/api/tenants", response_model=list[TenantOut])
def list_tenants(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[Tenant]:
    if current_user.is_platform_admin:
        return session.scalars(select(Tenant).order_by(Tenant.id)).all()
    if current_user.tenant_id is None:
        return []
    tenant = session.get(Tenant, current_user.tenant_id)
    return [tenant] if tenant else []


@router.post("/api/tenants", response_model=TenantOut)
def post_tenant(
    payload: TenantCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Tenant:
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    return create_tenant(session, payload)


@router.patch("/api/tenants/{tenant_id}", response_model=TenantOut)
def patch_tenant(
    tenant_id: int,
    payload: TenantUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Tenant:
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    try:
        return update_tenant(session, tenant_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/tenant-notification-settings", response_model=TenantNotificationSettingsOut)
def get_tenant_notification_settings(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    resolved_tenant_id = resolve_tenant_id(current_user, tenant_id)
    tenant = session.get(Tenant, resolved_tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant not found")
    return notification_settings_payload(tenant)


@router.patch("/api/tenant-notification-settings", response_model=TenantNotificationSettingsOut)
def patch_tenant_notification_settings(
    payload: TenantNotificationSettingsUpdate,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    resolved_tenant_id = resolve_tenant_id(current_user, tenant_id)
    try:
        return update_tenant_notification_settings(session, resolved_tenant_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/reports", response_model=ReportOut)
def reports(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    return build_report(session, resolve_tenant_id(current_user, tenant_id))


@router.post("/api/worker/drain-once")
def post_worker_drain_once(current_user: CurrentUser = Depends(get_current_user)) -> dict[str, int]:
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    require_core_feature_access(current_user)
    if get_settings().app_env == "production":
        raise forbidden("worker drain endpoint is disabled in production")
    return {"processed": drain_once()}
