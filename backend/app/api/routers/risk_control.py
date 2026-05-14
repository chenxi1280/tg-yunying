from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, require_core_feature_access
from app.database import get_session
from app.common.http import not_found
from app.schemas.risk_control import (
    AccountProxyCreate,
    AccountProxyOut,
    AccountProxyUpdate,
    ProxyAlertActionRequest,
    ProxyBatchBindingOut,
    ProxyBatchBindingRequest,
    ProxyBindingOut,
    ProxyBindingRequest,
    ProxyCheckRequest,
    ProxyDisableRequest,
    ProxyHealthCheckOut,
    RiskControlAccountScoreOut,
    RiskControlGlobalPolicyOut,
    RiskControlGlobalPolicyUpdate,
    RiskControlOverviewOut,
    RiskControlSummaryOut,
    RiskDispositionItemOut,
    RiskHitRecordOut,
    RiskPreflightOut,
    RiskPreflightRequest,
    RiskProxyAlertOut,
)
from app.services.risk_control import (
    bind_account_proxy,
    bind_accounts_proxy_batch,
    check_account_proxy,
    create_account_proxy,
    disable_account_proxy,
    list_account_proxies,
    list_proxy_alerts,
    risk_control_summary,
    risk_preflight,
    update_account_proxy,
    update_global_policy,
    update_proxy_alert_status,
)


router = APIRouter()


@router.get("/api/risk-control/summary", response_model=RiskControlSummaryOut)
def get_risk_control_summary(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return risk_control_summary(session, current_user.tenant_id or 1)


@router.get("/api/risk-control/overview", response_model=RiskControlOverviewOut)
def get_risk_control_overview(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return risk_control_summary(session, current_user.tenant_id or 1)["overview"]


@router.get("/api/risk-control/accounts", response_model=list[RiskControlAccountScoreOut])
def get_risk_control_accounts(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return risk_control_summary(session, current_user.tenant_id or 1)["account_scores"]


@router.get("/api/risk-control/accounts/{account_id}", response_model=RiskControlAccountScoreOut)
def get_risk_control_account(account_id: int, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    for item in risk_control_summary(session, current_user.tenant_id or 1)["account_scores"]:
        if item["account_id"] == account_id:
            return item
    raise not_found("account risk profile not found")


@router.get("/api/risk-control/events", response_model=list[RiskHitRecordOut])
def get_risk_control_events(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return risk_control_summary(session, current_user.tenant_id or 1)["hit_records"]


@router.get("/api/risk-control/dispositions", response_model=list[RiskDispositionItemOut])
def get_risk_control_dispositions(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return risk_control_summary(session, current_user.tenant_id or 1)["disposition_queue"]


@router.patch("/api/risk-control/global-policy", response_model=RiskControlGlobalPolicyOut)
def patch_risk_control_global_policy(payload: RiskControlGlobalPolicyUpdate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    require_core_feature_access(current_user)
    return update_global_policy(session, current_user.tenant_id or 1, payload, current_user.name)


@router.post("/api/risk-control/preflight", response_model=RiskPreflightOut)
def post_risk_control_preflight(payload: RiskPreflightRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    require_core_feature_access(current_user)
    return risk_preflight(session, current_user.tenant_id or 1, payload)


@router.get("/api/account-proxies", response_model=list[AccountProxyOut])
def get_account_proxies(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return list_account_proxies(session, current_user.tenant_id or 1)


@router.post("/api/account-proxies", response_model=AccountProxyOut)
def post_account_proxy(payload: AccountProxyCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    require_core_feature_access(current_user)
    try:
        return create_account_proxy(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/account-proxies/{proxy_id}", response_model=AccountProxyOut)
def patch_account_proxy(proxy_id: int, payload: AccountProxyUpdate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    require_core_feature_access(current_user)
    try:
        return update_account_proxy(session, current_user.tenant_id or 1, proxy_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/account-proxies/{proxy_id}/check", response_model=ProxyHealthCheckOut)
def post_account_proxy_check(proxy_id: int, payload: ProxyCheckRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    require_core_feature_access(current_user)
    try:
        return check_account_proxy(session, current_user.tenant_id or 1, proxy_id, check_type=payload.check_type, reason=payload.reason, actor=current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/account-proxies/{proxy_id}/disable", response_model=AccountProxyOut)
def post_account_proxy_disable(proxy_id: int, payload: ProxyDisableRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    require_core_feature_access(current_user)
    try:
        return disable_account_proxy(session, current_user.tenant_id or 1, proxy_id, payload.disabled_reason, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/accounts/{account_id}/proxy-binding", response_model=ProxyBindingOut)
@router.post("/api/tg-accounts/{account_id}/proxy-binding", response_model=ProxyBindingOut)
def post_account_proxy_binding(account_id: int, payload: ProxyBindingRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    require_core_feature_access(current_user)
    try:
        return bind_account_proxy(session, current_user.tenant_id or 1, account_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/accounts/proxy-bindings/batch", response_model=ProxyBatchBindingOut)
def post_account_proxy_bindings_batch(payload: ProxyBatchBindingRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    require_core_feature_access(current_user)
    return bind_accounts_proxy_batch(session, current_user.tenant_id or 1, payload, current_user.name)


@router.get("/api/proxy-alerts", response_model=list[RiskProxyAlertOut])
def get_proxy_alerts(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return list_proxy_alerts(session, current_user.tenant_id or 1)


@router.post("/api/proxy-alerts/{alert_id}/acknowledge", response_model=RiskProxyAlertOut)
def post_proxy_alert_acknowledge(alert_id: int, payload: ProxyAlertActionRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    require_core_feature_access(current_user)
    try:
        return update_proxy_alert_status(session, current_user.tenant_id or 1, alert_id, "acknowledged", current_user.name, reason=payload.reason)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/proxy-alerts/{alert_id}/ignore", response_model=RiskProxyAlertOut)
def post_proxy_alert_ignore(alert_id: int, payload: ProxyAlertActionRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    require_core_feature_access(current_user)
    try:
        return update_proxy_alert_status(session, current_user.tenant_id or 1, alert_id, "ignored", current_user.name, reason=payload.reason, ignored_until=payload.ignored_until)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/proxy-alerts/{alert_id}/resolve", response_model=RiskProxyAlertOut)
def post_proxy_alert_resolve(alert_id: int, payload: ProxyAlertActionRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    require_core_feature_access(current_user)
    try:
        return update_proxy_alert_status(session, current_user.tenant_id or 1, alert_id, "recovered", current_user.name, reason=payload.reason)
    except ValueError as exc:
        if "健康检查" in str(exc):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise not_found(str(exc)) from exc


__all__ = ["router"]
