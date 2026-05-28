from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TenantLearningProfileVersion, TenantLearningRun
from app.services._common import _now, audit
from app.services.tenant_target_profile import get_target_profile_overview, ensure_tenant_profile, latest_quality_rule


def list_profile_versions(session: Session, tenant_id: int) -> dict[str, Any]:
    rows = session.scalars(
        select(TenantLearningProfileVersion)
        .where(TenantLearningProfileVersion.tenant_id == tenant_id)
        .order_by(TenantLearningProfileVersion.profile_version.desc())
    ).all()
    return {"items": [_version_payload(row) for row in rows], "total": len(rows)}


def restore_profile_version(session: Session, tenant_id: int, version_id: str, *, actor: str, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("请填写恢复画像版本原因")
    version = session.get(TenantLearningProfileVersion, version_id)
    if not version or version.tenant_id != tenant_id:
        raise ValueError("画像版本不存在")
    profile = ensure_tenant_profile(session, tenant_id)
    snapshot = dict(version.profile_snapshot or {})
    profile.profile_version += 1
    profile.status = str(snapshot.get("status") or "sample_insufficient")
    profile.style_summary = str(snapshot.get("style_summary") or "")
    profile.topic_weights = dict(snapshot.get("topic_weights") or {})
    profile.phrase_patterns = list(snapshot.get("phrase_patterns") or [])
    profile.reply_patterns = list(snapshot.get("reply_patterns") or [])
    profile.comment_patterns = list(snapshot.get("comment_patterns") or [])
    profile.forbidden_learning = list(snapshot.get("forbidden_learning") or [])
    profile.source_sample_count = int(snapshot.get("source_sample_count") or 0)
    profile.last_rebuilt_at = _now()
    restored = _record_version(session, tenant_id, profile, actor, {"restored_from": version.id})
    run = TenantLearningRun(
        tenant_id=tenant_id,
        run_type="restore_version",
        status="success",
        profile_version=profile.profile_version,
        quality_rule_version=restored.quality_rule_version,
        trace_id=f"profile-restore-{profile.profile_version}",
    )
    session.add(run)
    audit(session, tenant_id=tenant_id, actor=actor, action="恢复全站目标画像版本", target_type="target_profile", target_id=str(tenant_id), detail=f"from_version={version_id}; reason={reason.strip()}")
    session.flush()
    return get_target_profile_overview(session, tenant_id)


def update_profile_settings(session: Session, tenant_id: int, payload: dict[str, Any], *, actor: str, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("请填写画像设置变更原因")
    profile = ensure_tenant_profile(session, tenant_id)
    if "learning_enabled" in payload:
        profile.learning_enabled = bool(payload["learning_enabled"])
    audit(session, tenant_id=tenant_id, actor=actor, action="配置全站目标画像设置", target_type="target_profile", target_id=str(tenant_id), detail=reason.strip())
    session.flush()
    return get_target_profile_overview(session, tenant_id)


def get_profile_run(session: Session, tenant_id: int, run_id: str) -> dict[str, Any]:
    run = session.get(TenantLearningRun, run_id)
    if not run or run.tenant_id != tenant_id:
        raise ValueError("学习运行不存在")
    return _run_payload(run)


def _record_version(session: Session, tenant_id: int, profile: Any, actor: str, source_snapshot: dict[str, Any]) -> TenantLearningProfileVersion:
    rule = latest_quality_rule(session, tenant_id)
    version = TenantLearningProfileVersion(
        tenant_id=tenant_id,
        profile_version=profile.profile_version,
        profile_snapshot={
            "profile_version": profile.profile_version,
            "status": profile.status,
            "style_summary": profile.style_summary,
            "topic_weights": profile.topic_weights or {},
            "phrase_patterns": profile.phrase_patterns or [],
            "reply_patterns": profile.reply_patterns or [],
            "comment_patterns": profile.comment_patterns or [],
            "forbidden_learning": profile.forbidden_learning or [],
            "source_sample_count": profile.source_sample_count,
        },
        source_snapshot=source_snapshot,
        quality_rule_version=rule.rule_version if rule else 0,
        sample_count=profile.source_sample_count,
        created_by=actor,
    )
    session.add(version)
    return version


def _version_payload(version: TenantLearningProfileVersion) -> dict[str, Any]:
    snapshot = version.profile_snapshot or {}
    return {
        "id": version.id,
        "profile_version": version.profile_version,
        "status": snapshot.get("status") or "",
        "style_summary": snapshot.get("style_summary") or "",
        "source_sample_count": version.sample_count,
        "quality_rule_version": version.quality_rule_version,
        "source_snapshot": version.source_snapshot or {},
        "created_by": version.created_by,
        "created_at": _iso(version.created_at),
    }


def _run_payload(run: TenantLearningRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "run_type": run.run_type,
        "source_id": run.source_id,
        "status": run.status,
        "pulled_count": run.pulled_count,
        "sample_count": run.sample_count,
        "accepted_count": run.accepted_count,
        "rejected_count": run.rejected_count,
        "quality_rule_version": run.quality_rule_version,
        "profile_version": run.profile_version,
        "failure_detail": run.failure_detail,
        "trace_id": run.trace_id,
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None
