from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import object_session

from app.auth import CurrentUser
from app.models import TgLoginFlow
from app.schemas import AccountOut, AccountRuntimeSummaryOut, AuditLogOut
from app.services.account_authorizations import authorization_summaries_for_accounts, authorization_summary_for_account


SENSITIVE_AUDIT_MARKERS = (
    "验证码",
    "密钥",
    "凭证",
    "api_hash",
    "api hash",
    "secret",
    "password",
)


def account_out_for_user(
    account: Any,
    current_user: CurrentUser,
    authorization_summary: dict[str, Any] | None = None,
    latest_login_flow: Any | None = None,
) -> dict[str, Any]:
    data = AccountOut.model_validate(account).model_dump()
    summary = authorization_summary or _authorization_summary_for_loaded_account(account)
    if summary:
        data["authorization_summary"] = summary
    flow = latest_login_flow if latest_login_flow is not None else _latest_login_flow_for_loaded_account(account)
    if flow:
        data["latest_login_flow"] = _login_flow_summary(flow)
    can_read_phone = _can_read_phone(current_user)
    if not can_read_phone:
        data["phone_number"] = None
    if not current_user.has_permission("accounts.sensitive.read"):
        data["developer_api_id"] = None
        data["proxy_local_address"] = None
    if not current_user.has_permission("accounts.security.read"):
        data["developer_app_health_status"] = None
        data["profile_sync_error"] = ""
        data["proxy_status"] = None
        data["proxy_alert_status"] = None
    return data


def accounts_out_for_user(accounts: list[Any], current_user: CurrentUser) -> list[dict[str, Any]]:
    summaries = _authorization_summaries_for_loaded_accounts(accounts)
    flows = _latest_login_flows_for_loaded_accounts(accounts)
    return [account_out_for_user(account, current_user, summaries.get(account.id), flows.get(account.id)) for account in accounts]


def _authorization_summary_for_loaded_account(account: Any) -> dict[str, Any] | None:
    session = object_session(account)
    if session is None:
        return None
    return authorization_summary_for_account(session, account)


def _authorization_summaries_for_loaded_accounts(accounts: list[Any]) -> dict[int, dict[str, Any]]:
    if not accounts:
        return {}
    session = object_session(accounts[0])
    if session is None:
        return {}
    return authorization_summaries_for_accounts(session, accounts)


def _latest_login_flow_for_loaded_account(account: Any) -> Any | None:
    session = object_session(account)
    if session is None:
        return None
    return session.scalar(
        select(TgLoginFlow)
        .where(TgLoginFlow.account_id == account.id)
        .order_by(TgLoginFlow.id.desc())
        .limit(1)
    )


def _latest_login_flows_for_loaded_accounts(accounts: list[Any]) -> dict[int, Any]:
    if not accounts:
        return {}
    session = object_session(accounts[0])
    if session is None:
        return {}
    account_ids = [account.id for account in accounts]
    latest_ids = (
        select(func.max(TgLoginFlow.id).label("id"))
        .where(TgLoginFlow.account_id.in_(account_ids))
        .group_by(TgLoginFlow.account_id)
        .subquery()
    )
    rows = session.scalars(select(TgLoginFlow).join(latest_ids, TgLoginFlow.id == latest_ids.c.id))
    return {row.account_id: row for row in rows}


def _login_flow_summary(flow: Any) -> dict[str, Any]:
    return {
        "method": flow.method,
        "status": flow.status,
        "failure_type": flow.failure_type,
        "failure_detail": flow.failure_detail,
        "trace_id": flow.trace_id,
        "created_at": flow.created_at,
    }


def _can_read_phone(current_user: CurrentUser) -> bool:
    return (
        current_user.has_permission("accounts.sensitive.read")
        or current_user.has_permission("accounts.create")
        or current_user.has_permission("accounts.login")
    )


def account_availability_out_for_user(summary: Any, current_user: CurrentUser) -> dict[str, Any]:
    data = AccountRuntimeSummaryOut.model_validate(summary).model_dump()
    capacity_limit = int(data.get("capacity_limit") or 100)
    capacity_used = max(0, capacity_limit - int(data.get("remaining_capacity") or 0))
    data["capacity_limit"] = capacity_limit
    data["capacity_used"] = capacity_used
    data["capacity_explanation"] = (
        f"剩余容量 = {capacity_limit} - 当前待处理/领取中/执行中/可重试/结果未知占用 {capacity_used}；"
        "调度前仍会按账号冷却、全局限额和风控实时复检。"
    )
    if not current_user.has_permission("accounts.security.read"):
        data["unavailable_reason"] = "已隐藏敏感状态" if data.get("unavailable_reason") else ""
        data["failure_trend"] = {}
        data["next_retry_at"] = None
    return data


def account_detail_out_for_user(detail: dict[str, Any], current_user: CurrentUser) -> dict[str, Any]:
    data = dict(detail)
    data["account"] = account_out_for_user(data["account"], current_user)
    if not current_user.has_permission("accounts.codes.read"):
        data["verification_codes"] = []
    return data


def account_pool_detail_out_for_user(detail: dict[str, Any], current_user: CurrentUser) -> dict[str, Any]:
    data = dict(detail)
    data["accounts"] = accounts_out_for_user(data.get("accounts", []), current_user)
    return data


def audit_log_out_for_user(log: Any, current_user: CurrentUser) -> dict[str, Any]:
    data = AuditLogOut.model_validate(log).model_dump()
    if current_user.has_permission("audits.view_sensitive"):
        return data
    haystack = f"{data.get('action', '')} {data.get('detail', '')}".lower()
    if any(marker.lower() in haystack for marker in SENSITIVE_AUDIT_MARKERS):
        data["detail"] = "已隐藏敏感详情"
    return data
