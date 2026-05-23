from __future__ import annotations

import csv
import json
from io import StringIO

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, resolve_tenant_id
from app.database import get_session
from app.schemas.archives import ReportOut
from app.schemas.operations_center import (
    ListenerErrorOut,
    ListenerEventOut,
    ListenerSummaryOut,
    ListenerSwitchRequest,
    ListenerWatermarkResetRequest,
    OperationMetricsExportRequest,
    OperationMetricsOut,
    RelayAttributionReportOut,
    RuleCenterSummaryOut,
    RuleSetCreate,
    RuleSetBoundTaskOut,
    RuleSetOut,
    RuleSetVersionActionRequest,
    RuleSetVersionCreate,
    RuleTestOut,
    RuleTestRequest,
)
from app.schemas.runtime_summary import (
    OperationCenterOverviewOut,
    OperationIssueDetailOut,
    OperationIssueOut,
    OperationIssueStatusRequest,
    TargetRuntimeSummaryOut,
)
from app.services.operations_center import (
    copy_rule_set_version,
    create_rule_set,
    create_rule_set_version,
    list_listener_errors,
    list_listener_events,
    listener_summary,
    list_rule_set_bound_tasks,
    list_rule_sets,
    operation_metrics_summary,
    publish_rule_set_version,
    relay_attribution_csv,
    relay_attribution_report,
    reset_listener_watermark,
    rule_center_summary,
    rollback_rule_set_version,
    switch_listener_account,
    test_rules,
    update_rule_set_config,
)
from app.services._common import audit
from app.services.reports import build_report
from app.services.runtime_summary import (
    acknowledge_operation_issue,
    claim_operation_issue,
    get_operation_issue_detail,
    ignore_operation_issue,
    list_target_runtime_summaries,
    list_operation_issues,
    operation_center_overview,
    resolve_operation_issue,
)
from app.common.http import not_found


router = APIRouter()
SYSTEM_SCOPE_ID = 1


def _operation_report_csv(report: dict) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["section", "key", "value"])
    for section, payload in report.items():
        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                writer.writerow([section, key, value])
        else:
            writer.writerow([section, "", payload])
    return output.getvalue()


@router.get("/api/listeners/summary", response_model=ListenerSummaryOut)
def get_listener_summary(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return listener_summary(session, current_user.tenant_id or 1)


@router.post("/api/listeners/{object_type}/{object_id}/switch", response_model=ListenerSummaryOut)
def post_listener_switch(object_type: str, object_id: int, payload: ListenerSwitchRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return switch_listener_account(session, current_user.tenant_id or 1, object_type, object_id, payload.backup_account_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/listeners/{object_type}/{object_id}/reset-watermark", response_model=ListenerSummaryOut)
def post_listener_reset_watermark(object_type: str, object_id: int, payload: ListenerWatermarkResetRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return reset_listener_watermark(session, current_user.tenant_id or 1, object_type, object_id, reason=payload.reason, actor=current_user.name, confirm_text=payload.confirm_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/listeners/{object_type}/{object_id}/events", response_model=list[ListenerEventOut])
def get_listener_events(object_type: str, object_id: int, limit: int = 50, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return list_listener_events(session, current_user.tenant_id or 1, object_type, object_id, limit=limit)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/listeners/{object_type}/{object_id}/errors", response_model=list[ListenerErrorOut])
def get_listener_errors(object_type: str, object_id: int, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return list_listener_errors(session, current_user.tenant_id or 1, object_type, object_id)
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


@router.get("/api/operation-metrics/reports", response_model=ReportOut)
def get_operation_metrics_reports(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return build_report(session, resolve_tenant_id(current_user, tenant_id))


@router.post("/api/operation-metrics/export")
def post_operation_metrics_export(
    payload: OperationMetricsExportRequest,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Response:
    resolved_tenant_id = resolve_tenant_id(current_user, tenant_id)
    report = build_report(session, resolved_tenant_id)
    audit(
        session,
        tenant_id=resolved_tenant_id,
        actor=current_user.name,
        action="导出运营指标",
        target_type="operation_metrics",
        target_id="export",
        detail=f"reason={payload.reason}",
    )
    session.commit()
    return Response(
        content=_operation_report_csv(report),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="operation-metrics.csv"'},
    )


@router.get("/api/operation-center/overview", response_model=OperationCenterOverviewOut)
def get_operation_center_overview(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return operation_center_overview(session, current_user.tenant_id or 1)


@router.get("/api/operation-targets/runtime-summary", response_model=list[TargetRuntimeSummaryOut])
def get_operation_target_runtime_summaries(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return list_target_runtime_summaries(session, current_user.tenant_id or 1)


@router.get("/api/operation-issues", response_model=list[OperationIssueOut])
def get_operation_issues(
    target_id: int | None = None,
    issue_type: str | None = None,
    severity: str | None = None,
    status: str | None = "open",
    failure_type: str | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return list_operation_issues(
        session,
        current_user.tenant_id or 1,
        target_id=target_id,
        issue_type=issue_type,
        severity=severity,
        status=status,
        failure_type=failure_type,
    )


@router.get("/api/operation-issues/{issue_id}", response_model=OperationIssueDetailOut)
def get_operation_issue(issue_id: str, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return get_operation_issue_detail(session, current_user.tenant_id or 1, issue_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/operation-issues/{issue_id}/acknowledge", response_model=OperationIssueOut)
def post_operation_issue_acknowledge(
    issue_id: str,
    payload: OperationIssueStatusRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        issue = acknowledge_operation_issue(session, current_user.tenant_id or 1, issue_id, current_user.name, payload.reason)
        session.commit()
        session.refresh(issue)
        return issue
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/operation-issues/{issue_id}/claim", response_model=OperationIssueOut)
def post_operation_issue_claim(
    issue_id: str,
    payload: OperationIssueStatusRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        issue = claim_operation_issue(session, current_user.tenant_id or 1, issue_id, current_user.name, payload.reason)
        session.commit()
        session.refresh(issue)
        return issue
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/operation-issues/{issue_id}/resolve", response_model=OperationIssueOut)
def post_operation_issue_resolve(
    issue_id: str,
    payload: OperationIssueStatusRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        issue = resolve_operation_issue(session, current_user.tenant_id or 1, issue_id, payload.reason, current_user.name)
        session.commit()
        session.refresh(issue)
        return issue
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/operation-issues/{issue_id}/ignore", response_model=OperationIssueOut)
def post_operation_issue_ignore(
    issue_id: str,
    payload: OperationIssueStatusRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        issue = ignore_operation_issue(session, current_user.tenant_id or 1, issue_id, current_user.name, payload.reason)
        session.commit()
        session.refresh(issue)
        return issue
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/rules/test", response_model=RuleTestOut)
def post_rule_test(payload: RuleTestRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return test_rules(
        session,
        SYSTEM_SCOPE_ID,
        payload.text,
        test_type=payload.test_type,
        test_mode=payload.test_mode,
        simulation_scenario=payload.simulation_scenario,
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
def post_rule_set_version_publish(rule_set_id: int, version_id: int, payload: RuleSetVersionActionRequest | None = None, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return publish_rule_set_version(session, SYSTEM_SCOPE_ID, rule_set_id, version_id, current_user.name, reason=payload.reason if payload else "")
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/rule-sets/{rule_set_id}/versions/{version_id}/copy", response_model=RuleSetOut)
def post_rule_set_version_copy(rule_set_id: int, version_id: int, payload: RuleSetVersionActionRequest | None = None, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return copy_rule_set_version(session, SYSTEM_SCOPE_ID, rule_set_id, version_id, current_user.name, reason=payload.reason if payload else "")
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/rule-sets/{rule_set_id}/versions/{version_id}/rollback", response_model=RuleSetOut)
def post_rule_set_version_rollback(rule_set_id: int, version_id: int, payload: RuleSetVersionActionRequest | None = None, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return rollback_rule_set_version(session, SYSTEM_SCOPE_ID, rule_set_id, version_id, current_user.name, reason=payload.reason if payload else "")
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/rule-sets/{rule_set_id}/tasks", response_model=list[RuleSetBoundTaskOut])
def get_rule_set_bound_tasks(rule_set_id: int, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return list_rule_set_bound_tasks(session, SYSTEM_SCOPE_ID, rule_set_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


__all__ = ["router"]
