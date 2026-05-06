"""Developer app management routes."""
from __future__ import annotations


from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user
from app.database import get_session
from app.common.http import forbidden, not_found
from app.schemas import DeveloperAppCreate, DeveloperAppOut, DeveloperAppUpdate
from app.services import (
    check_developer_app, create_developer_app, list_developer_apps,
    set_developer_app_active, update_developer_app,
)

router = APIRouter()


@router.get("/api/developer-apps", response_model=list[DeveloperAppOut])
def get_developer_apps(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    return list_developer_apps(session)


@router.post("/api/developer-apps", response_model=DeveloperAppOut)
def post_developer_app(
    payload: DeveloperAppCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    try:
        return create_developer_app(session, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.patch("/api/developer-apps/{app_id}", response_model=DeveloperAppOut)
def patch_developer_app(
    app_id: int,
    payload: DeveloperAppUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    try:
        return update_developer_app(session, app_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/developer-apps/{app_id}/check", response_model=DeveloperAppOut)
def post_developer_app_check(
    app_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    try:
        return check_developer_app(session, app_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/developer-apps/{app_id}/disable", response_model=DeveloperAppOut)
def post_developer_app_disable(
    app_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    try:
        return set_developer_app_active(session, app_id, False, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/developer-apps/{app_id}/enable", response_model=DeveloperAppOut)
def post_developer_app_enable(
    app_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    try:
        return set_developer_app_active(session, app_id, True, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc
