from __future__ import annotations

from fastapi import HTTPException

from app.auth import CurrentUser


def not_found(message: str) -> HTTPException:
    return HTTPException(status_code=404, detail=message)


def forbidden(message: str = "forbidden") -> HTTPException:
    return HTTPException(status_code=403, detail=message)


def ensure_tenant_access(current_user: CurrentUser, tenant_id: int | None) -> None:
    if current_user.is_platform_admin:
        return
    if tenant_id != current_user.tenant_id:
        raise forbidden("cross-tenant access denied")
