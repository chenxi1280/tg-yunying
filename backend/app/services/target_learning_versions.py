from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OperationTarget, TargetLearningProfile, TargetLearningProfileVersion
from app.services._common import _now, audit


def list_learning_profile_versions(session: Session, tenant_id: int, target_id: int, profile_scene: str) -> dict:
    profile = _profile_for_scene(session, tenant_id, target_id, profile_scene)
    if not profile:
        return {"target_id": target_id, "profile_scene": profile_scene, "items": []}
    rows = session.scalars(
        select(TargetLearningProfileVersion)
        .where(TargetLearningProfileVersion.tenant_id == tenant_id, TargetLearningProfileVersion.profile_id == profile.id)
        .order_by(TargetLearningProfileVersion.profile_version.desc(), TargetLearningProfileVersion.created_at.desc())
    ).all()
    return {"target_id": target_id, "profile_scene": profile_scene, "items": [_version_payload(row) for row in rows]}


def restore_learning_profile_version(session: Session, tenant_id: int, target_id: int, version_id: str, *, actor: str, reason: str) -> TargetLearningProfile:
    if not reason.strip():
        raise ValueError("请填写恢复原因")
    version = session.get(TargetLearningProfileVersion, version_id)
    if not version or version.tenant_id != tenant_id:
        raise ValueError("画像版本不存在")
    profile = session.get(TargetLearningProfile, version.profile_id)
    if not profile or profile.tenant_id != tenant_id or profile.target_id != target_id:
        raise ValueError("画像版本不属于当前目标")
    snapshot = version.summary_snapshot or {}
    _apply_snapshot(profile, snapshot)
    restored_from = version.profile_version
    profile.profile_version = int(profile.profile_version or 0) + 1
    profile.last_rebuilt_at = _now()
    session.add(
        TargetLearningProfileVersion(
            tenant_id=tenant_id,
            profile_id=profile.id,
            profile_version=profile.profile_version,
            source_sample_count=profile.source_sample_count,
            summary_snapshot=_profile_snapshot(profile),
            quality_snapshot={"restored_from": restored_from},
            created_by=actor,
        )
    )
    audit(session, tenant_id=tenant_id, actor=actor, action="恢复目标画像版本", target_type="target_learning_profile", target_id=profile.id, detail=f"from_version={restored_from}; reason={reason.strip()}")
    return profile


def record_profile_version_snapshot(session: Session, profile: TargetLearningProfile, actor: str, quality_snapshot: dict | None = None) -> None:
    session.add(
        TargetLearningProfileVersion(
            tenant_id=profile.tenant_id,
            profile_id=profile.id,
            profile_version=profile.profile_version,
            source_sample_count=profile.source_sample_count,
            summary_snapshot=_profile_snapshot(profile),
            quality_snapshot=quality_snapshot or {},
            created_by=actor,
        )
    )


def _profile_for_scene(session: Session, tenant_id: int, target_id: int, profile_scene: str) -> TargetLearningProfile | None:
    target = session.get(OperationTarget, target_id)
    if not target or target.tenant_id != tenant_id:
        raise ValueError("运营目标不存在")
    return session.scalar(select(TargetLearningProfile).where(TargetLearningProfile.tenant_id == tenant_id, TargetLearningProfile.target_id == target_id, TargetLearningProfile.profile_scene == profile_scene))


def _apply_snapshot(profile: TargetLearningProfile, snapshot: dict) -> None:
    profile.style_summary = str(snapshot.get("style_summary") or "")
    profile.topic_weights = snapshot.get("topic_weights") or {}
    profile.phrase_patterns = snapshot.get("phrase_patterns") or []
    profile.reply_patterns = snapshot.get("reply_patterns") or []
    profile.comment_patterns = snapshot.get("comment_patterns") or []
    profile.forbidden_learning = snapshot.get("forbidden_learning") or []
    profile.source_sample_count = int(snapshot.get("source_sample_count") or 0)


def _profile_snapshot(profile: TargetLearningProfile) -> dict:
    return {
        "target_id": profile.target_id,
        "profile_id": profile.id,
        "profile_scene": profile.profile_scene,
        "profile_version": profile.profile_version,
        "style_summary": profile.style_summary,
        "topic_weights": profile.topic_weights or {},
        "phrase_patterns": profile.phrase_patterns or [],
        "reply_patterns": profile.reply_patterns or [],
        "comment_patterns": profile.comment_patterns or [],
        "forbidden_learning": profile.forbidden_learning or [],
        "source_sample_count": profile.source_sample_count,
        "last_rebuilt_at": profile.last_rebuilt_at.isoformat() if profile.last_rebuilt_at else "",
    }


def _version_payload(version: TargetLearningProfileVersion) -> dict:
    return {
        "id": version.id,
        "profile_version": version.profile_version,
        "source_sample_count": version.source_sample_count,
        "summary_snapshot": version.summary_snapshot or {},
        "quality_snapshot": version.quality_snapshot or {},
        "created_by": version.created_by,
        "created_at": version.created_at.isoformat() if version.created_at else "",
    }
