from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.common.http import ensure_tenant_access, not_found


def require_resource_tenant(session: Session, current_user: CurrentUser, model, resource_id: int) -> None:
    resource = session.get(model, resource_id)
    if not resource:
        raise not_found("resource not found")
    ensure_tenant_access(current_user, getattr(resource, "tenant_id", None))
