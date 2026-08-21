from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, resolve_tenant_id
from app.common.http import forbidden
from app.database import get_session
from app.schemas.authorization_dr import (
    DrLocalActivateApplyRequest,
    DrLocalActivateOut,
    DrLocalActivatePreviewRequest,
)
from app.services.authorization_dr import (
    AuthorizationDrError,
    apply_local_activate,
    local_activate_out,
    preview_local_activate,
)

from .authorization_dr import _dr_http_error


router = APIRouter()


def _require_authorization_manage(user: CurrentUser) -> None:
    if not user.has_permission("accounts.authorizations.manage"):
        raise forbidden("accounts.authorizations.manage required")


@router.post(
    "/api/tg-accounts/{account_id}/authorizations/{authorization_id}/local-activate/preview",
    response_model=DrLocalActivateOut,
)
def post_local_activate_preview(
    account_id: int,
    authorization_id: int,
    payload: DrLocalActivatePreviewRequest,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_authorization_manage(current_user)
    try:
        case = preview_local_activate(
            session,
            resolve_tenant_id(current_user, tenant_id),
            account_id,
            authorization_id,
            actor=current_user.name,
            reason=payload.reason,
        )
        return local_activate_out(case)
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


@router.post(
    "/api/tg-accounts/{account_id}/authorizations/{authorization_id}/local-activate",
    response_model=DrLocalActivateOut,
)
def post_local_activate_apply(
    account_id: int,
    authorization_id: int,
    payload: DrLocalActivateApplyRequest,
    tenant_id: int | None = None,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_authorization_manage(current_user)
    try:
        case = apply_local_activate(
            session,
            resolve_tenant_id(current_user, tenant_id),
            account_id,
            authorization_id,
            fingerprint=payload.fingerprint,
            actor=current_user.name,
            approval_ref=payload.approval_ref,
            idempotency_key=idempotency_key,
        )
        return local_activate_out(case)
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


__all__ = ["router"]
