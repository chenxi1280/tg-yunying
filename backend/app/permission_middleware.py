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
    _compile("GET", r"^/api/config/runtime$", "system.view"),
    _compile("GET", r"^/api/tg-accounts/\d+/verification-codes$", "accounts.codes.read"),
    _compile("POST", r"^/api/tg-accounts/\d+/verification-codes/poll$", "accounts.codes.read"),
    _compile("GET", r"^/api/tg-accounts/security/summary$", "accounts.security.read"),
    _compile("GET", r"^/api/tg-accounts/\d+/security$", "accounts.security.read"),
    _compile("GET", r"^/api/tg-accounts/security-batches(?:/.*)?$", "accounts.security.read"),
    _compile("GET", r"^/api/tg-accounts/\d+/verification-tasks$", "accounts.sync"),
    _compile("GET", r"^/api/tg-accounts(?:/.*)?$", "accounts.view"),
    _compile("GET", r"^/api/account-pools(?:/.*)?$", "accounts.view"),
    _compile("GET", r"^/api/groups/\d+/verification-tasks$", "accounts.sync"),
    _compile("GET", r"^/api/groups(?:/.*)?$", "targets.view"),
    _compile("GET", r"^/api/target-profile(?:/.*)?$", "target_profile.view"),
    _compile("GET", r"^/api/operation-targets(?:/.*)?$", "targets.view"),
    _compile("GET", r"^/api/operation-plans(?:/.*)?$", "operation_plans.manage"),
    _compile("GET", r"^/api/channel-comments(?:/.*)?$", "targets.view"),
    _compile("GET", r"^/api/channel-messages(?:/.*)?$", "targets.view"),
    _compile("GET", r"^/api/message-(?:send-)?tasks(?:/.*)?$", "message_sending.view"),
    _compile("GET", r"^/api/tasks(?:/.*)?$", "tasks.view"),
    _compile("GET", r"^/api/operation-tasks(?:/.*)?$", "tasks.view"),
    _compile("GET", r"^/api/review-queue(?:/.*)?$", "tasks.view"),
    _compile("GET", r"^/api/listeners(?:/.*)?$", "listeners.view"),
    _compile("GET", r"^/api/operation-center/overview$", "overview.view"),
    _compile("GET", r"^/api/operation-issues(?:/.*)?$", ("overview.view", "operation_issues.manage")),
    _compile("GET", r"^/api/rule(?:-|_)?sets(?:/.*)?$", "rules.view"),
    _compile("GET", r"^/api/rules/summary$", "rules.view"),
    _compile("GET", r"^/api/risk-control(?:/.*)?$", "risk.view"),
    _compile("GET", r"^/api/account-proxies(?:/.*)?$", "risk.view"),
    _compile("GET", r"^/api/proxy-alerts(?:/.*)?$", "risk.view"),
    _compile("GET", r"^/api/archives(?:/.*)?$", "archives.view"),
    _compile("GET", r"^/api/operation-metrics(?:/.*)?$", "usage.view"),
    _compile("GET", r"^/api/reports$", "usage.view"),
    _compile("GET", r"^/api/materials/cache/(?:health|config)$", ("materials.view", "system.view")),
    _compile("GET", r"^/api/material-groups(?:/.*)?$", "materials.view"),
    _compile("GET", r"^/api/materials(?:/.*)?$", "materials.view"),
    _compile("GET", r"^/api/material-imports(?:/.*)?$", "materials.view"),
    _compile("GET", r"^/api/audit-logs/export$", "audit.export"),
    _compile("GET", r"^/api/audit-logs(?:/.*)?$", "audits.view"),
    _compile("GET", r"^/api/audits(?:/.*)?$", "audits.view"),
    _compile("GET", r"^/api/tenants(?:/.*)?$", "system.view"),
    _compile("GET", r"^/api/tenant-(?:notification|group-rescue)-settings$", "system.view"),
    _compile("GET", r"^/api/(?:developer-apps|ai-providers|prompt-templates|tenant-ai-settings|content-keyword-rules|scheduling-settings)(?:/.*)?$", "system.view"),
    _compile("GET", r"^/api/ai-account-voice-profiles(?:/.*)?$", ("system.view", "ai_voice_profiles.manage")),
    _compile("GET", r"^/api/admin/users(?:/.*)?$", "permissions.view"),
    _compile("GET", r"^/api/account-clone-plans(?:/.*)?$", "accounts.clone"),
    _compile("GET", r"^/api/verification-tasks(?:/.*)?$", "accounts.sync"),
    _compile("GET", r"^/api/rules/relay-attribution/report$", "rules.view"),
    _compile("GET", r"^/api/(?:campaigns|ai-drafts)(?:/.*)?$", "message_sending.view"),
    _compile("GET", r"^/api/(?:operation-task-attempts|manual-operation-records)$", "tasks.view"),

    _compile("POST", r"^/api/tg-accounts$", "accounts.create"),
    _compile("POST", r"^/api/tg-accounts/availability/rebuild$", "accounts.sync"),
    _compile("POST", r"^/api/tg-accounts/\d+/login(?:/.*)?$", "accounts.login"),
    _compile("POST", r"^/api/tg-accounts/\d+/authorizations(?:/.*)?$", "accounts.authorizations.manage"),
    _compile("POST", r"^/api/tg-accounts/\d+/(?:sync-groups|sync-now|sync-targets|contacts/sync|health-check|profile-sync/retry)$", "accounts.sync"),
    _compile("POST", r"^/api/tg-accounts/\d+/pending-execution/recheck$", "accounts.sync"),
    _compile("PATCH", r"^/api/tg-accounts/\d+/profile$", "accounts.profile.batch_update"),
    _compile("PATCH", r"^/api/tg-accounts/\d+/identity$", "accounts.pool_manage"),
    _compile("POST", r"^/api/tg-accounts/\d+/avatar$", "accounts.profile.batch_update"),
    _compile("POST", r"^/api/tg-accounts/\d+/security/refresh$", "accounts.security.read"),
    _compile("POST", r"^/api/tg-accounts/\d+/security/(?:cleanup-devices|set-2fa)$", "accounts.security.batch"),
    _compile("POST", r"^/api/tg-accounts/\d+/security/managed-2fa(?:/(?:rotate|reveal))?$", "accounts.security.credential_manage"),
    _compile("POST", r"^/api/tg-accounts/\d+/security/update-profile$", "accounts.profile.batch_update"),
    _compile("POST", r"^/api/tg-accounts/security-batches/profile-preview$", "accounts.profile.batch_update"),
    _compile("POST", r"^/api/tg-accounts/security-batches/precheck$", ("accounts.security.batch", "accounts.profile.batch_update", "accounts.security.session_manage")),
    _compile("POST", r"^/api/tg-accounts/security-batches(?:/.*)?$", ("accounts.security.batch", "accounts.profile.batch_update", "accounts.security.session_manage")),
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
    _compile("POST", r"^/api/ai-providers(?:/.*)?$", "ai.manage"),
    _compile("PATCH", r"^/api/ai-providers(?:/.*)?$", "ai.manage"),
    _compile("POST", r"^/api/ai-account-voice-profiles(?:/.*)?$", "ai_voice_profiles.manage"),
    _compile("PATCH", r"^/api/ai-account-voice-profiles(?:/.*)?$", "ai_voice_profiles.manage"),
    _compile("POST", r"^/api/materials/\d+/(?:disable|restore)$", "materials.manage"),
    _compile("POST", r"^/api/materials/\d+/(?:versions|refresh-cache)$", "materials.manage"),
    _compile("POST", r"^/api/materials(?:/upload(?:/(?:batch|zip))?)?$", "materials.upload"),
    _compile("POST", r"^/api/material-groups(?:/.*)?$", "materials.manage"),
    _compile("PATCH", r"^/api/material-groups(?:/.*)?$", "materials.manage"),
    _compile("PATCH", r"^/api/materials/cache/config$", "system.manage"),
    _compile("PATCH", r"^/api/materials(?:/.*)?$", "materials.manage"),
    _compile("POST", r"^/api/prompt-templates(?:/.*)?$", "prompt_templates.manage"),
    _compile("PATCH", r"^/api/prompt-templates(?:/.*)?$", "prompt_templates.manage"),
    _compile("PATCH", r"^/api/tenant-ai-settings(?:/.*)?$", "ai.manage"),
    _compile("POST", r"^/api/content-keyword-rules(?:/.*)?$", "ai.manage"),
    _compile("PATCH", r"^/api/(?:content-keyword-rules|scheduling-settings)(?:/.*)?$", "ai.manage"),
    _compile("POST", r"^/api/tenants(?:/.*)?$", "system.manage"),
    _compile("PATCH", r"^/api/tenants(?:/.*)?$", "system.manage"),
    _compile("PATCH", r"^/api/tenant-notification-settings$", "system.manage"),
    _compile("PATCH", r"^/api/tenant-bot-settings$", "system.manage"),
    _compile("POST", r"^/api/tenant-bot-settings/test-message$", "system.manage"),
    _compile("POST", r"^/api/tenant-bot-settings/webhook/refresh$", "system.manage"),
    _compile("DELETE", r"^/api/tenant-bot-settings/webhook$", "system.manage"),
    _compile("PATCH", r"^/api/tenant-group-rescue-settings$", "system.manage"),
    _compile("POST", r"^/api/telegram-bot/tasks/group-ai-chat/settings$", "system.manage"),
    _compile("POST", r"^/api/telegram-bot/update$", "system.manage"),
    _compile("POST", r"^/api/worker/drain-once$", "tasks.dispatch_control"),

    _compile("POST", r"^/api/target-profile(?:/.*)?$", "target_profile.manage"),
    _compile("PATCH", r"^/api/target-profile(?:/.*)?$", "target_profile.manage"),
    _compile("PUT", r"^/api/target-profile(?:/.*)?$", "target_profile.manage"),
    _compile("POST", r"^/api/operation-targets(?:/.*)?$", "targets.manage"),
    _compile("POST", r"^/api/operation-issues(?:/.*)?$", "operation_issues.manage"),
    _compile("POST", r"^/api/operation-plans(?:/.*)?$", "operation_plans.manage"),
    _compile("PATCH", r"^/api/operation-plans(?:/.*)?$", "operation_plans.manage"),
    _compile("PATCH", r"^/api/operation-targets(?:/.*)?$", "targets.manage"),
    _compile("POST", r"^/api/channel-messages(?:/.*)?$", "targets.manage"),
    _compile("PATCH", r"^/api/groups(?:/.*)?$", "targets.manage"),
    _compile("POST", r"^/api/groups(?:/.*)?$", "targets.manage"),
    _compile("POST", r"^/api/verification-tasks(?:/.*)?$", "accounts.sync"),

    _compile("POST", r"^/api/message-(?:send-)?tasks(?:/.*)?$", "message_sending.manage"),
    _compile("POST", r"^/api/(?:campaigns|ai-drafts)(?:/.*)?$", "message_sending.manage"),
    _compile("PATCH", r"^/api/ai-drafts(?:/.*)?$", "message_sending.manage"),
    _compile("POST", r"^/api/operation-metrics/export$", "usage.export"),
    _compile("POST", r"^/api/operation-tasks(?:/.*)?$", "tasks.manage"),
    _compile("POST", r"^/api/review/[^/]+/(?:approve|reject)$", "tasks.manage"),
    _compile("POST", r"^/api/tasks/[^/]+/reset$", "tasks.dispatch_control"),
    _compile("POST", r"^/api/tasks(?:/.*)?$", "tasks.manage"),
    _compile("PATCH", r"^/api/tasks(?:/.*)?$", "tasks.manage"),
    _compile("DELETE", r"^/api/tasks(?:/.*)?$", "tasks.manage"),
    _compile("POST", r"^/api/listeners(?:/.*)?$", "listeners.manage"),
    _compile("POST", r"^/api/rules/test$", "rules.publish"),
    _compile("POST", r"^/api/rule-sets(?:/.*)?$", "rules.publish"),
    _compile("PUT", r"^/api/rule-sets(?:/.*)?$", "rules.publish"),
    _compile("PATCH", r"^/api/risk-control(?:/.*)?$", "risk.manage"),
    _compile("POST", r"^/api/risk-control(?:/.*)?$", "risk.manage"),
    _compile("POST", r"^/api/account-proxies(?:/.*)?$", "proxies.manage"),
    _compile("PATCH", r"^/api/account-proxies(?:/.*)?$", "proxies.manage"),
    _compile("POST", r"^/api/(?:accounts|tg-accounts)/\d+/proxy-binding$", "proxies.manage"),
    _compile("POST", r"^/api/accounts/proxy-bindings/batch$", "proxies.manage"),
    _compile("POST", r"^/api/proxy-alerts(?:/.*)?$", "proxies.manage"),
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
            has_allowed_permission = any(current_user.has_permission(permission) for permission in permissions)
            if not has_allowed_permission:
                missing_permissions = list(permissions)
                _audit_permission_denied(session, current_user, request, missing_permissions)
                return JSONResponse({"detail": "permission denied", "permission": ",".join(missing_permissions)}, status_code=403)
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)
