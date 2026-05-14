from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user
from app.database import get_session
from app.schemas.operations_center import (
    ListenerSummaryOut,
    ListenerSwitchRequest,
    OperationMetricsOut,
    RelayAttributionReportOut,
    RuleCenterSummaryOut,
    RuleSetCreate,
    RuleSetBoundTaskOut,
    RuleSetOut,
    RuleSetVersionCreate,
    RuleTestOut,
    RuleTestRequest,
)
from app.services.operations_center import (
    copy_rule_set_version,
    create_rule_set,
    create_rule_set_version,
    listener_summary,
    list_rule_set_bound_tasks,
    list_rule_sets,
    operation_metrics_summary,
    publish_rule_set_version,
    relay_attribution_csv,
    relay_attribution_report,
    rule_center_summary,
    rollback_rule_set_version,
    switch_listener_account,
    test_rules,
    update_rule_set_config,
)
from app.common.http import not_found


router = APIRouter()
SYSTEM_SCOPE_ID = 1


@router.get("/api/listeners/summary", response_model=ListenerSummaryOut)
def get_listener_summary(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return listener_summary(session, current_user.tenant_id or 1)


@router.post("/api/listeners/{object_type}/{object_id}/switch", response_model=ListenerSummaryOut)
def post_listener_switch(object_type: str, object_id: int, payload: ListenerSwitchRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return switch_listener_account(session, current_user.tenant_id or 1, object_type, object_id, payload.backup_account_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/rules/summary", response_model=RuleCenterSummaryOut)
def get_rule_summary(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return rule_center_summary(session, SYSTEM_SCOPE_ID)


@router.get("/api/rules/relay-attribution/export")
def export_relay_attribution(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)) -> Response:
    return Response(
        content=relay_attribution_csv(session, SYSTEM_SCOPE_ID),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="relay-attribution.csv"'},
    )


@router.get("/api/rules/relay-attribution/report", response_model=RelayAttributionReportOut)
def get_relay_attribution_report(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return relay_attribution_report(session, SYSTEM_SCOPE_ID)


@router.get("/api/operation-metrics/summary", response_model=OperationMetricsOut)
def get_operation_metrics_summary(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return operation_metrics_summary(session, current_user.tenant_id or 1)


@router.post("/api/rules/test", response_model=RuleTestOut)
def post_rule_test(payload: RuleTestRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return test_rules(
        session,
        SYSTEM_SCOPE_ID,
        payload.text,
        test_type=payload.test_type,
        test_mode=payload.test_mode,
        candidates=payload.candidates,
        context=payload.context,
        rule_set_version_id=payload.rule_set_version_id,
        source_group_id=payload.source_group_id,
        sender_id=payload.sender_id,
        message_type=payload.message_type,
    )


@router.get("/api/rule-sets", response_model=list[RuleSetOut])
def get_rule_sets(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return list_rule_sets(session, SYSTEM_SCOPE_ID)


@router.post("/api/rule-sets", response_model=RuleSetOut)
def post_rule_set(payload: RuleSetCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_rule_set(session, SYSTEM_SCOPE_ID, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/rule-sets/{rule_set_id}/versions", response_model=RuleSetOut)
def post_rule_set_version(rule_set_id: int, payload: RuleSetVersionCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_rule_set_version(session, SYSTEM_SCOPE_ID, rule_set_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.put("/api/rule-sets/{rule_set_id}/config", response_model=RuleSetOut)
def put_rule_set_config(rule_set_id: int, payload: RuleSetVersionCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return update_rule_set_config(session, SYSTEM_SCOPE_ID, rule_set_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/rule-sets/{rule_set_id}/versions/{version_id}/publish", response_model=RuleSetOut)
def post_rule_set_version_publish(rule_set_id: int, version_id: int, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return publish_rule_set_version(session, SYSTEM_SCOPE_ID, rule_set_id, version_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/rule-sets/{rule_set_id}/versions/{version_id}/copy", response_model=RuleSetOut)
def post_rule_set_version_copy(rule_set_id: int, version_id: int, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return copy_rule_set_version(session, SYSTEM_SCOPE_ID, rule_set_id, version_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/rule-sets/{rule_set_id}/versions/{version_id}/rollback", response_model=RuleSetOut)
def post_rule_set_version_rollback(rule_set_id: int, version_id: int, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return rollback_rule_set_version(session, SYSTEM_SCOPE_ID, rule_set_id, version_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/rule-sets/{rule_set_id}/tasks", response_model=list[RuleSetBoundTaskOut])
def get_rule_set_bound_tasks(rule_set_id: int, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return list_rule_set_bound_tasks(session, SYSTEM_SCOPE_ID, rule_set_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


__all__ = ["router"]
