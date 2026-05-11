from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user
from app.database import get_session
from app.schemas.operations_center import (
    ListenerSummaryOut,
    OperationMetricsOut,
    RuleCenterSummaryOut,
    RuleSetCreate,
    RuleSetOut,
    RuleSetVersionCreate,
    RuleTestOut,
    RuleTestRequest,
)
from app.services.operations_center import (
    create_rule_set,
    create_rule_set_version,
    listener_summary,
    list_rule_sets,
    operation_metrics_summary,
    publish_rule_set_version,
    rule_center_summary,
    test_rules,
)
from app.common.http import not_found


router = APIRouter()


@router.get("/api/listeners/summary", response_model=ListenerSummaryOut)
def get_listener_summary(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return listener_summary(session, current_user.tenant_id or 1)


@router.get("/api/rules/summary", response_model=RuleCenterSummaryOut)
def get_rule_summary(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return rule_center_summary(session, current_user.tenant_id or 1)


@router.get("/api/operation-metrics/summary", response_model=OperationMetricsOut)
def get_operation_metrics_summary(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return operation_metrics_summary(session, current_user.tenant_id or 1)


@router.post("/api/rules/test", response_model=RuleTestOut)
def post_rule_test(payload: RuleTestRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return test_rules(session, current_user.tenant_id or 1, payload.text)


@router.get("/api/rule-sets", response_model=list[RuleSetOut])
def get_rule_sets(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return list_rule_sets(session, current_user.tenant_id or 1)


@router.post("/api/rule-sets", response_model=RuleSetOut)
def post_rule_set(payload: RuleSetCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_rule_set(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/rule-sets/{rule_set_id}/versions", response_model=RuleSetOut)
def post_rule_set_version(rule_set_id: int, payload: RuleSetVersionCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_rule_set_version(session, current_user.tenant_id or 1, rule_set_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/rule-sets/{rule_set_id}/versions/{version_id}/publish", response_model=RuleSetOut)
def post_rule_set_version_publish(rule_set_id: int, version_id: int, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return publish_rule_set_version(session, current_user.tenant_id or 1, rule_set_id, version_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


__all__ = ["router"]
