from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, ensure_permission, get_current_user
from app.database import get_session as get_db
from app.models import Task
from app.schemas.task_center import GroupCloneCutoverRequest, GroupCloneRollbackRequest
from app.services.task_center.group_clone_cutover import (
    apply_clone_cutover,
    apply_clone_rollback,
    preview_clone_cutover,
    preview_clone_rollback,
)
from app.services.task_center.group_clone_lifecycle import tenant_clone_task

router = APIRouter(prefix="/api/tasks", tags=["group_clone"])


@router.post("/{legacy_task_id}/group-clone/cutover/preview")
def preview_cutover_to_clone(
    legacy_task_id: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    ensure_permission(current_user, "tasks.manage")
    legacy_task = _legacy_task(db, current_user, legacy_task_id, for_update=False)
    try:
        result = preview_clone_cutover(db, legacy_task, actor_id=current_user.id)
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{legacy_task_id}/group-clone/cutover/apply")
def apply_cutover_to_clone(
    legacy_task_id: str,
    *,
    req: GroupCloneCutoverRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    ensure_permission(current_user, "tasks.manage")
    legacy_task = _legacy_task(db, current_user, legacy_task_id, for_update=True)
    try:
        result = apply_clone_cutover(
            db, legacy_task, request=req, actor_id=current_user.id,
        )
        db.commit()
        return result
    except (ValueError, RuntimeError) as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{clone_task_id}/group-clone/rollback/preview")
def preview_rollback_to_relay(
    clone_task_id: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    ensure_permission(current_user, "tasks.manage")
    clone = _clone_task(db, current_user, clone_task_id)
    try:
        result = preview_clone_rollback(db, clone, actor_id=current_user.id)
        db.commit()
        return result
    except (ValueError, RuntimeError) as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{clone_task_id}/group-clone/rollback/apply")
def apply_rollback_to_relay(
    clone_task_id: str,
    *,
    req: GroupCloneRollbackRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    ensure_permission(current_user, "tasks.manage")
    if req.clone_task_id != clone_task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="clone_task_id 与路径不一致",
        )
    clone = _clone_task(db, current_user, clone_task_id)
    try:
        result = apply_clone_rollback(
            db, clone, request=req, actor_id=current_user.id,
        )
        db.commit()
        return result
    except (ValueError, RuntimeError) as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _legacy_task(session, current_user, task_id: str, *, for_update: bool) -> Task:
    statement = select(Task).where(
        Task.id == task_id,
        Task.tenant_id == (current_user.tenant_id or 1),
        Task.type == "group_relay",
        Task.deleted_at.is_(None),
    )
    task = session.scalar(statement.with_for_update() if for_update else statement)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="单 route 监听转发任务不存在",
        )
    return task


def _clone_task(session, current_user, task_id: str) -> Task:
    try:
        return tenant_clone_task(session, current_user.tenant_id or 1, task_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


__all__ = ["router"]
