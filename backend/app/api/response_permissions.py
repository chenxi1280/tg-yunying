from __future__ import annotations

from typing import Any

from app.auth import CurrentUser
from app.schemas import AccountOut, AuditLogOut


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
    return AccountOut.model_validate(account).model_dump()


def account_detail_out_for_user(detail: dict[str, Any], current_user: CurrentUser) -> dict[str, Any]:
    data = dict(detail)
    data["account"] = account_out_for_user(data["account"], current_user)
    if not current_user.has_permission("accounts.view_codes"):
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
