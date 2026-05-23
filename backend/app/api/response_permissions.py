from __future__ import annotations

from typing import Any

from app.auth import CurrentUser
from app.schemas import AccountOut, AccountRuntimeSummaryOut, AuditLogOut


SENSITIVE_AUDIT_MARKERS = (
    "验证码",
    "密钥",
    "凭证",
    "api_hash",
    "api hash",
    "secret",
    "password",
)


def account_out_for_user(account: Any, current_user: CurrentUser) -> dict[str, Any]:
    data = AccountOut.model_validate(account).model_dump()
    can_read_phone = (
        current_user.has_permission("accounts.sensitive.read")
        or current_user.has_permission("accounts.create")
        or current_user.has_permission("accounts.login")
    )
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


def account_availability_out_for_user(summary: Any, current_user: CurrentUser) -> dict[str, Any]:
    data = AccountRuntimeSummaryOut.model_validate(summary).model_dump()
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
    data["accounts"] = [account_out_for_user(account, current_user) for account in data.get("accounts", [])]
    return data


def audit_log_out_for_user(log: Any, current_user: CurrentUser) -> dict[str, Any]:
    data = AuditLogOut.model_validate(log).model_dump()
    if current_user.has_permission("audits.view_sensitive"):
        return data
    haystack = f"{data.get('action', '')} {data.get('detail', '')}".lower()
    if any(marker.lower() in haystack for marker in SENSITIVE_AUDIT_MARKERS):
        data["detail"] = "已隐藏敏感详情"
    return data
