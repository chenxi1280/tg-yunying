from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, Response
from starlette.responses import JSONResponse

from .auth import current_user_from_authorization
from .database import SessionLocal
from .models import AuditLog


PermissionSet = tuple[str, ...]
PermissionRule = tuple[str, re.Pattern[str], PermissionSet]


def _compile(method: str, pattern: str, permission: str | PermissionSet) -> PermissionRule:
    permissions = (permission,) if isinstance(permission, str) else permission
    return method, re.compile(pattern), permissions


PERMISSION_RULES: list[PermissionRule] = [
    _compile("GET", r"^/api/overview$", "overview.view"),
    _compile("GET", r"^/api/tg-accounts/\d+/verification-codes$", "accounts.view_codes"),
    _compile("POST", r"^/api/tg-accounts/\d+/verification-codes/poll$", "accounts.view_codes"),
    _compile("GET", r"^/api/tg-accounts(?:/.*)?$", "accounts.view"),
    _compile("GET", r"^/api/account-pools(?:/.*)?$", "accounts.view"),
    _compile("GET", r"^/api/groups(?:/.*)?$", "targets.view"),
    _compile("GET", r"^/api/operation-targets(?:/.*)?$", "targets.view"),
    _compile("GET", r"^/api/channel-messages(?:/.*)?$", "targets.view"),
    _compile("GET", r"^/api/message-tasks(?:/.*)?$", "message_sending.view"),
    _compile("GET", r"^/api/tasks(?:/.*)?$", "tasks.view"),
    _compile("GET", r"^/api/listeners(?:/.*)?$", "listeners.view"),
    _compile("GET", r"^/api/rule(?:-|_)?sets(?:/.*)?$", "rules.view"),
    _compile("GET", r"^/api/rules/summary$", "rules.view"),
    _compile("GET", r"^/api/risk-control(?:/.*)?$", "risk.view"),
    _compile("GET", r"^/api/account-proxies(?:/.*)?$", "risk.view"),
    _compile("GET", r"^/api/proxy-alerts(?:/.*)?$", "risk.view"),
    _compile("GET", r"^/api/archives(?:/.*)?$", "archives.view"),
    _compile("GET", r"^/api/operation-metrics(?:/.*)?$", "usage.view"),
    _compile("GET", r"^/api/reports$", "usage.view"),
    _compile("GET", r"^/api/audit-logs/export$", "audits.view"),
    _compile("GET", r"^/api/audit-logs(?:/.*)?$", "audits.view"),
    _compile("GET", r"^/api/audits(?:/.*)?$", "audits.view"),
    _compile("GET", r"^/api/tenants(?:/.*)?$", "system.view"),
    _compile("GET", r"^/api/(?:developer-apps|ai-providers|prompt-templates|tenant-ai-settings|materials|content-keyword-rules|scheduling-settings)(?:/.*)?$", "system.view"),
    _compile("GET", r"^/api/admin/users(?:/.*)?$", "permissions.view"),

    _compile("POST", r"^/api/tg-accounts$", "accounts.create"),
    _compile("POST", r"^/api/tg-accounts/\d+/login(?:/.*)?$", "accounts.login"),
    _compile("POST", r"^/api/tg-accounts/\d+/(?:sync-groups|sync-now|sync-targets|contacts/sync|health-check|profile-sync/retry)$", "accounts.sync"),
    _compile("PATCH", r"^/api/tg-accounts/\d+/profile$", "accounts.update_profile"),
    _compile("POST", r"^/api/tg-accounts/\d+/avatar$", "accounts.update_profile"),
    _compile("DELETE", r"^/api/tg-accounts/\d+$", "accounts.delete"),
    _compile("POST", r"^/api/tg-accounts/\d+/move-pool$", "accounts.pool_manage"),
    _compile("POST", r"^/api/account-pools(?:/.*)?$", "accounts.pool_manage"),
    _compile("PATCH", r"^/api/account-pools(?:/.*)?$", "accounts.pool_manage"),
    _compile("POST", r"^/api/account-clone-plans(?:/.*)?$", "accounts.clone"),
    _compile("POST", r"^/api/account-clone-items(?:/.*)?$", "accounts.clone"),
    _compile("POST", r"^/api/tg-accounts/\d+/(?:manual-send|direct-message-tasks)$", "accounts.manual_send"),
    _compile("POST", r"^/api/account-pools/\d+/direct-message-tasks$", "accounts.manual_send"),

    _compile("POST", r"^/api/developer-apps$", "developer_apps.manage"),
    _compile("POST", r"^/api/developer-apps/\d+/(?:check|disable|enable)$", "developer_apps.manage"),
    _compile("POST", r"^/api/developer-apps(?:/.*)?$", "developer_apps.manage"),
    _compile("PATCH", r"^/api/developer-apps(?:/.*)?$", "developer_apps.manage"),
    _compile("POST", r"^/api/ai-providers(?:/.*)?$", "system.secrets_manage"),
    _compile("PATCH", r"^/api/ai-providers(?:/.*)?$", "system.secrets_manage"),
    _compile("POST", r"^/api/(?:prompt-templates|materials|content-keyword-rules)(?:/.*)?$", "system.manage"),
    _compile("PATCH", r"^/api/(?:prompt-templates|tenant-ai-settings|materials|content-keyword-rules|scheduling-settings)(?:/.*)?$", "system.manage"),
    _compile("POST", r"^/api/tenants(?:/.*)?$", "system.manage"),
    _compile("PATCH", r"^/api/tenants(?:/.*)?$", "system.manage"),
    _compile("PATCH", r"^/api/tenant-notification-settings$", "system.secrets_manage"),
    _compile("POST", r"^/api/worker/drain-once$", "system.manage"),

    _compile("POST", r"^/api/operation-targets(?:/.*)?$", "targets.manage"),
    _compile("PATCH", r"^/api/operation-targets(?:/.*)?$", "targets.manage"),
    _compile("POST", r"^/api/channel-messages(?:/.*)?$", "targets.manage"),
    _compile("PATCH", r"^/api/groups(?:/.*)?$", "targets.manage"),
    _compile("POST", r"^/api/groups(?:/.*)?$", "targets.manage"),
    _compile("POST", r"^/api/verification-tasks(?:/.*)?$", "accounts.sync"),

    _compile("POST", r"^/api/message-(?:send-)?tasks(?:/.*)?$", "message_sending.create"),
    _compile("POST", r"^/api/tasks(?:/.*)?$", "tasks.manage"),
    _compile("PATCH", r"^/api/tasks(?:/.*)?$", "tasks.manage"),
    _compile("DELETE", r"^/api/tasks(?:/.*)?$", "tasks.manage"),
    _compile("POST", r"^/api/listeners(?:/.*)?$", "listeners.manage"),
    _compile("POST", r"^/api/rules/test$", "rules.publish"),
    _compile("POST", r"^/api/rule-sets(?:/.*)?$", "rules.publish"),
    _compile("PUT", r"^/api/rule-sets(?:/.*)?$", "rules.publish"),
    _compile("PATCH", r"^/api/risk-control(?:/.*)?$", "risk.manage"),
    _compile("POST", r"^/api/risk-control(?:/.*)?$", "risk.manage"),
    _compile("POST", r"^/api/account-proxies(?:/.*)?$", "risk.manage"),
    _compile("PATCH", r"^/api/account-proxies(?:/.*)?$", "risk.manage"),
    _compile("POST", r"^/api/(?:accounts|tg-accounts)/\d+/proxy-binding$", "accounts.proxy_bind"),
    _compile("POST", r"^/api/accounts/proxy-bindings/batch$", "accounts.proxy_bind"),
    _compile("POST", r"^/api/proxy-alerts(?:/.*)?$", "risk.manage"),
    _compile("POST", r"^/api/archives/\d+/export$", "archives.export"),
    _compile("POST", r"^/api/archives(?:/.*)?$", "archives.manage"),
    _compile("GET", r"^/api/rules/relay-attribution/export$", "rules.view"),
    _compile("POST", r"^/api/admin/users(?:/.*)?$", "permissions.manage"),
    _compile("PATCH", r"^/api/admin/users(?:/.*)?$", "permissions.manage"),
]


def required_permission(method: str, path: str) -> PermissionSet | None:
    for rule_method, pattern, permission in PERMISSION_RULES:
        if rule_method == method and pattern.match(path):
            return permission
    return None


def _audit_permission_denied(
    session,
    current_user,
    request: Request,
    missing_permissions: list[str],
) -> None:
    try:
        actor = f"{current_user.name}#{current_user.id}({current_user.role_template or current_user.role})"
        session.add(
            AuditLog(
                tenant_id=current_user.tenant_id,
                actor=actor,
                action="权限拒绝",
                target_type="permission",
                target_id=",".join(missing_permissions),
                detail=f"method={request.method}; path={request.url.path}; missing={','.join(missing_permissions)}",
                ip_address=request.client.host if request.client else "",
            )
        )
        session.commit()
    except Exception:
        session.rollback()


async def permission_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    if request.method == "OPTIONS":
        return await call_next(request)
    permissions = required_permission(request.method, request.url.path)
    if permissions is None:
        return await call_next(request)
    try:
        with SessionLocal() as session:
            current_user = current_user_from_authorization(request.headers.get("authorization"), session)
            missing_permissions = [permission for permission in permissions if not current_user.has_permission(permission)]
            if missing_permissions:
                _audit_permission_denied(session, current_user, request, missing_permissions)
                return JSONResponse({"detail": "permission denied", "permission": ",".join(missing_permissions)}, status_code=403)
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)
