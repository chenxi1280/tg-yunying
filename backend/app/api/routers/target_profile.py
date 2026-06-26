from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import CurrentUser, ensure_permission, get_current_user
from app.database import get_session
from app.services.tenant_target_profile import (
    TargetProfileRunFailed,
    clear_profile,
    get_quality_rules,
    get_target_profile_overview,
    list_runs,
    list_samples,
    list_source_candidates,
    list_sources,
    recompute_candidates,
    rebuild_profile,
    start_source_run,
    target_profile_usage,
    update_sample_status,
    update_quality_rules,
    update_sources,
)
from app.services.tenant_target_profile_admin import get_profile_run, list_profile_versions, restore_profile_version, update_profile_settings


router = APIRouter()


@router.get("/api/target-profile")
def get_target_profile(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.view")
    profile = get_target_profile_overview(session, current_user.tenant_id or 1)
    session.commit()
    return profile


@router.get("/api/target-profile/usage")
def get_target_profile_usage(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.view")
    return target_profile_usage(session, current_user.tenant_id or 1)


@router.get("/api/target-profile/source-candidates")
def get_target_profile_source_candidates(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.view")
    return list_source_candidates(session, current_user.tenant_id or 1)


@router.get("/api/target-profile/sources")
def get_target_profile_sources(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.view")
    return list_sources(session, current_user.tenant_id or 1)


@router.put("/api/target-profile/sources")
def put_target_profile_sources(payload: dict, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.manage")
    try:
        result = update_sources(session, current_user.tenant_id or 1, payload, actor=current_user.name, reason=str(payload.get("reason") or ""))
        session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/target-profile/sources/{source_id}/sync")
def post_target_profile_source_sync(source_id: str, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.manage")
    try:
        result = start_source_run(session, current_user.tenant_id or 1, source_id, "sync", actor=current_user.name)
        session.commit()
        return result
    except TargetProfileRunFailed as exc:
        session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/target-profile/sources/{source_id}/pull-history")
def post_target_profile_source_pull_history(source_id: str, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.manage")
    try:
        result = start_source_run(session, current_user.tenant_id or 1, source_id, "pull_history", actor=current_user.name)
        session.commit()
        return result
    except TargetProfileRunFailed as exc:
        session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/target-profile/runs")
def get_target_profile_runs(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.view")
    return list_runs(session, current_user.tenant_id or 1)


@router.get("/api/target-profile/runs/{run_id}")
def get_target_profile_run(run_id: str, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.view")
    try:
        return get_profile_run(session, current_user.tenant_id or 1, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/target-profile/samples")
def get_target_profile_samples(learning_status: str = "", session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.view")
    return list_samples(session, current_user.tenant_id or 1, {"learning_status": learning_status})


@router.patch("/api/target-profile/samples/{sample_id}")
def patch_target_profile_sample(sample_id: str, payload: dict, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.manage")
    try:
        result = update_sample_status(session, current_user.tenant_id or 1, sample_id, str(payload.get("learning_status") or ""), actor=current_user.name, reason=str(payload.get("reason") or ""))
        session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/target-profile/quality-rules")
def get_target_profile_quality_rules(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.view")
    result = get_quality_rules(session, current_user.tenant_id or 1)
    session.commit()
    return result


@router.patch("/api/target-profile/quality-rules")
def patch_target_profile_quality_rules(payload: dict, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.manage")
    try:
        result = update_quality_rules(session, current_user.tenant_id or 1, payload, actor=current_user.name, reason=str(payload.get("reason") or ""))
        session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/target-profile/recompute-candidates")
def post_target_profile_recompute_candidates(payload: dict, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.manage")
    try:
        result = recompute_candidates(session, current_user.tenant_id or 1, actor=current_user.name, reason=str(payload.get("reason") or ""))
        session.commit()
        return result
    except TargetProfileRunFailed as exc:
        session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/target-profile/versions")
def get_target_profile_versions(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.view")
    return list_profile_versions(session, current_user.tenant_id or 1)


@router.post("/api/target-profile/versions/{version_id}/restore")
def post_target_profile_version_restore(version_id: str, payload: dict, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.manage")
    try:
        result = restore_profile_version(session, current_user.tenant_id or 1, version_id, actor=current_user.name, reason=str(payload.get("reason") or ""))
        session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/target-profile/settings")
def patch_target_profile_settings(payload: dict, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.manage")
    try:
        result = update_profile_settings(session, current_user.tenant_id or 1, payload, actor=current_user.name, reason=str(payload.get("reason") or ""))
        session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/target-profile/rebuild")
def post_target_profile_rebuild(payload: dict, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.manage")
    try:
        result = rebuild_profile(session, current_user.tenant_id or 1, actor=current_user.name, reason=str(payload.get("reason") or ""))
        session.commit()
        return result
    except TargetProfileRunFailed as exc:
        session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/target-profile/clear")
def post_target_profile_clear(payload: dict, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_profile.manage")
    try:
        result = clear_profile(session, current_user.tenant_id or 1, actor=current_user.name, reason=str(payload.get("reason") or ""))
        session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
