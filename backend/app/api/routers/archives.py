"""Archive routes."""
from __future__ import annotations


from collections.abc import Sequence

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, require_core_feature_access, resolve_tenant_id
from app.database import get_session
from app.common.http import not_found
from app.models import GroupArchive
from app.repositories.tenant import require_resource_tenant
from app.schemas import ArchiveCreate, ArchiveDetailOut, ArchiveExportOut, ArchiveExportRequest, ArchiveOut
from app.services import create_archive, export_archive, get_archive_detail, rerun_archive

router = APIRouter()


@router.get("/api/archives", response_model=list[ArchiveOut])
def list_archives(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[GroupArchive]:
    tenant_id = resolve_tenant_id(current_user, tenant_id)
    return session.scalars(select(GroupArchive).where(GroupArchive.tenant_id == tenant_id).order_by(GroupArchive.id.desc())).all()


@router.post("/api/archives", response_model=ArchiveOut)
def post_archive(
    payload: ArchiveCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        tenant_id = resolve_tenant_id(current_user, payload.tenant_id)
        return create_archive(session, payload.model_copy(update={"tenant_id": tenant_id}))
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/archives/{archive_id}", response_model=ArchiveDetailOut)
def get_archive(
    archive_id: int,
    message_search: str | None = None,
    member_search: str | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        require_resource_tenant(session, current_user, GroupArchive, archive_id)
        return get_archive_detail(session, archive_id, message_search=message_search, member_search=member_search)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/archives/{archive_id}/rerun", response_model=ArchiveOut)
def post_archive_rerun(
    archive_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, GroupArchive, archive_id)
        return rerun_archive(session, archive_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/archives/{archive_id}/export", response_model=ArchiveExportOut)
def post_archive_export(
    archive_id: int,
    payload: ArchiveExportRequest | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, GroupArchive, archive_id)
        export_format = payload.export_format if payload else "json"
        return export_archive(session, archive_id, current_user.name, export_format)
    except ValueError as exc:
        raise not_found(str(exc)) from exc
