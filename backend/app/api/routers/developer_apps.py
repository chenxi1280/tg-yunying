"""Developer app management routes."""
from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import CurrentUser, ensure_permission, get_current_user
from app.database import get_session
from app.common.http import not_found
from app.schemas import (
    DeveloperAppCreate,
    DeveloperAppOut,
    DeveloperAppSlotAssignmentsUpdate,
    DeveloperAppUpdate,
)
from app.services import (
    DeveloperAppAssignmentVersionConflict, check_developer_app, create_developer_app, list_developer_apps,
    set_developer_app_active, update_developer_app, update_developer_app_slot_assignments,
)

router = APIRouter()


@router.get("/api/developer-apps", response_model=list[DeveloperAppOut])
def get_developer_apps(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    return list_developer_apps(session)


@router.post("/api/developer-apps", response_model=DeveloperAppOut)
def post_developer_app(
    payload: DeveloperAppCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    ensure_permission(current_user, "developer_apps.manage")
    try:
        return create_developer_app(session, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.put("/api/developer-apps/slot-assignments", response_model=list[DeveloperAppOut])
def put_developer_app_slot_assignments(
    payload: DeveloperAppSlotAssignmentsUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    ensure_permission(current_user, "developer_apps.manage")
    try:
        return update_developer_app_slot_assignments(session, payload, current_user.name)
    except DeveloperAppAssignmentVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/developer-apps/{app_id}", response_model=DeveloperAppOut)
def patch_developer_app(
    app_id: int,
    payload: DeveloperAppUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    ensure_permission(current_user, "developer_apps.manage")
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
    ensure_permission(current_user, "developer_apps.manage")
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
    ensure_permission(current_user, "developer_apps.manage")
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
    ensure_permission(current_user, "developer_apps.manage")
    try:
        return set_developer_app_active(session, app_id, True, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc
