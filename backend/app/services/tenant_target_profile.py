from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Task,
    TenantLearningProfile,
    TenantLearningProfileVersion,
    TenantLearningQualityRule,
    TenantLearningRun,
    TenantLearningSample,
    TenantLearningSource,
)
from app.services._common import _now, audit
from app.services.tenant_target_profile_sources import list_source_candidates, list_sources, update_sources


USAGE_SCOPE = ["group_ai_chat", "channel_comment", "discussion_reply"]
DEFAULT_RULES = {
    "identity_filters": {"exclude_bots": True, "exclude_managed_accounts": True},
    "text_filters": {"min_length": 2, "max_length": 4000, "keywords": []},
    "template_filters": {"similarity_threshold": 0.92, "phrases": []},
    "scoring_thresholds": {"accepted": 80, "downweighted": 40},
    "scene_weights": {"group_chat": 1.0, "channel_comment": 1.0, "discussion_reply": 1.0},
    "forbidden_patterns": {"keywords": [], "links": True, "contacts": True},
}


class TargetProfileRunFailed(ValueError):
    pass


def get_target_profile_overview(session: Session, tenant_id: int) -> dict[str, Any]:
    profile = ensure_tenant_profile(session, tenant_id)
    rule = latest_quality_rule(session, tenant_id)
    source_count = int(session.scalar(select(func.count()).select_from(TenantLearningSource).where(TenantLearningSource.tenant_id == tenant_id)) or 0)
    return {
        "profile_id": profile.id,
        "tenant_id": tenant_id,
        "profile_version": profile.profile_version,
        "status": profile.status,
        "learning_enabled": profile.learning_enabled,
        "usage_scope": USAGE_SCOPE,
        "style_summary": profile.style_summary,
        "topic_weights": profile.topic_weights or {},
        "phrase_patterns": profile.phrase_patterns or [],
        "reply_patterns": profile.reply_patterns or [],
        "comment_patterns": profile.comment_patterns or [],
        "forbidden_learning": profile.forbidden_learning or [],
        "source_sample_count": profile.source_sample_count,
        "source_count": source_count,
        "quality_rule_version": rule.rule_version if rule else 0,
        "last_rebuilt_at": _iso(profile.last_rebuilt_at),
        "last_used_at": _iso(profile.last_used_at),
        "available_for_ai": profile.learning_enabled and profile.source_sample_count > 0,
    }


def tenant_learning_profile_preview(session: Session, tenant_id: int, profile_scene: str) -> dict[str, Any]:
    profile = session.scalar(select(TenantLearningProfile).where(TenantLearningProfile.tenant_id == tenant_id))
    if not profile:
        return _unavailable_preview(profile_scene, "profile_missing")
    if not profile.learning_enabled:
        return _profile_preview_payload(profile, profile_scene, "learning_disabled")
    if profile.source_sample_count <= 0:
        return _profile_preview_payload(profile, profile_scene, "sample_insufficient")
    if profile.status != "active":
        return _profile_preview_payload(profile, profile_scene, profile.status)
    return _profile_preview_payload(profile, profile_scene, "")


def get_quality_rules(session: Session, tenant_id: int) -> dict[str, Any]:
    rule = ensure_quality_rule(session, tenant_id)
    return _quality_rule_payload(rule)


def update_quality_rules(session: Session, tenant_id: int, payload: dict[str, Any], *, actor: str, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("请填写质量规则变更原因")
    current = latest_quality_rule(session, tenant_id)
    version = (current.rule_version + 1) if current else 1
    rule = TenantLearningQualityRule(
        tenant_id=tenant_id,
        rule_version=version,
        identity_filters=dict(payload.get("identity_filters") or (current.identity_filters if current else DEFAULT_RULES["identity_filters"])),
        text_filters=dict(payload.get("text_filters") or (current.text_filters if current else DEFAULT_RULES["text_filters"])),
        template_filters=dict(payload.get("template_filters") or (current.template_filters if current else DEFAULT_RULES["template_filters"])),
        scoring_thresholds=dict(payload.get("scoring_thresholds") or (current.scoring_thresholds if current else DEFAULT_RULES["scoring_thresholds"])),
        scene_weights=dict(payload.get("scene_weights") or (current.scene_weights if current else DEFAULT_RULES["scene_weights"])),
        forbidden_patterns=dict(payload.get("forbidden_patterns") or (current.forbidden_patterns if current else DEFAULT_RULES["forbidden_patterns"])),
        updated_by=actor,
        updated_at=_now(),
    )
    session.add(rule)
    session.flush()
    audit(session, tenant_id=tenant_id, actor=actor, action="配置目标画像样本质量规则", target_type="target_profile", target_id=str(tenant_id), detail=reason.strip())
    session.flush()
    return _quality_rule_payload(rule)


def recompute_candidates(session: Session, tenant_id: int, *, actor: str, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("请填写重算候选原因")
    from app.services.tenant_learning_samples import recompute_source_candidates

    rule = ensure_quality_rule(session, tenant_id)
    try:
        counts = recompute_source_candidates(session, tenant_id)
    except Exception as exc:
        run = _record_failed_run(
            session,
            tenant_id,
            run_type="recompute_candidates",
            failure_detail=str(exc),
            trace_id=f"candidate-recompute-{rule.rule_version}-failed",
            quality_rule_version=rule.rule_version,
        )
        audit(session, tenant_id=tenant_id, actor=actor, action="重算目标画像候选样本失败", target_type="target_profile_run", target_id=run.id, detail=run.failure_detail)
        raise TargetProfileRunFailed(run.failure_detail) from exc
    run = TenantLearningRun(
        tenant_id=tenant_id,
        run_type="recompute_candidates",
        status="success",
        quality_rule_version=rule.rule_version,
        sample_count=counts["sample_count"],
        accepted_count=counts["accepted_count"],
        rejected_count=counts["rejected_count"],
        trace_id=f"candidate-recompute-{rule.rule_version}",
    )
    session.add(run)
    audit(session, tenant_id=tenant_id, actor=actor, action="重算目标画像候选样本", target_type="target_profile", target_id=str(tenant_id), detail=reason.strip())
    session.flush()
    return _run_payload(run)


def list_samples(session: Session, tenant_id: int, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    filters = filters or {}
    stmt = select(TenantLearningSample).where(TenantLearningSample.tenant_id == tenant_id)
    if filters.get("learning_status"):
        stmt = stmt.where(TenantLearningSample.learning_status == str(filters["learning_status"]))
    total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    samples = session.scalars(stmt.order_by(TenantLearningSample.created_at.desc()).limit(int(filters.get("page_size") or 50))).all()
    return {"items": [_sample_payload(sample) for sample in samples], "total": total}


def update_sample_status(session: Session, tenant_id: int, sample_id: str, status: str, *, actor: str, reason: str) -> dict[str, Any]:
    if status not in {"candidate", "accepted", "downweighted", "rejected"}:
        raise ValueError("样本状态不支持")
    if not reason.strip():
        raise ValueError("请填写样本调整原因")
    sample = session.get(TenantLearningSample, sample_id)
    if not sample or sample.tenant_id != tenant_id:
        raise ValueError("学习样本不存在")
    sample.learning_status = status
    sample.decision_by = actor
    sample.decision_at = _now()
    if status == "rejected":
        sample.reject_reason = reason.strip()
    if status == "downweighted":
        sample.downweight_reason = reason.strip()
    audit(session, tenant_id=tenant_id, actor=actor, action="调整全站画像样本状态", target_type="target_profile_sample", target_id=sample.id, detail=f"status={status}; reason={reason.strip()}")
    session.flush()
    return _sample_payload(sample)


def rebuild_profile(session: Session, tenant_id: int, *, actor: str, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("请填写重建原因")
    profile = ensure_tenant_profile(session, tenant_id)
    try:
        accepted = session.scalars(select(TenantLearningSample).where(TenantLearningSample.tenant_id == tenant_id, TenantLearningSample.learning_status == "accepted").order_by(TenantLearningSample.created_at.desc())).all()
    except Exception as exc:
        next_version = int(profile.profile_version or 0) + 1
        run = _record_failed_run(
            session,
            tenant_id,
            run_type="rebuild",
            failure_detail=str(exc),
            trace_id=f"profile-rebuild-{next_version}-failed",
            profile_version=next_version,
            quality_rule_version=_latest_quality_rule_version(session, tenant_id),
        )
        audit(session, tenant_id=tenant_id, actor=actor, action="重建全站目标画像失败", target_type="target_profile_run", target_id=run.id, detail=run.failure_detail)
        raise TargetProfileRunFailed(run.failure_detail) from exc
    profile.profile_version += 1
    profile.source_sample_count = len(accepted)
    profile.status = "active" if accepted else "sample_insufficient"
    profile.style_summary = _build_style_summary(accepted)
    profile.last_rebuilt_at = _now()
    version = TenantLearningProfileVersion(
        tenant_id=tenant_id,
        profile_version=profile.profile_version,
        profile_snapshot=_profile_snapshot(profile),
        source_snapshot={"accepted_sample_ids": [sample.id for sample in accepted]},
        quality_rule_version=(latest_quality_rule(session, tenant_id).rule_version if latest_quality_rule(session, tenant_id) else 0),
        sample_count=len(accepted),
        created_by=actor,
    )
    run = TenantLearningRun(
        tenant_id=tenant_id,
        run_type="rebuild",
        status="success",
        sample_count=len(accepted),
        accepted_count=len(accepted),
        profile_version=profile.profile_version,
        quality_rule_version=version.quality_rule_version,
        trace_id=f"profile-rebuild-{profile.profile_version}",
    )
    session.add_all([version, run])
    audit(session, tenant_id=tenant_id, actor=actor, action="重建全站目标画像", target_type="target_profile", target_id=str(tenant_id), detail=reason.strip())
    session.flush()
    return get_target_profile_overview(session, tenant_id)


def clear_profile(session: Session, tenant_id: int, *, actor: str, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("请填写清空原因")
    profile = ensure_tenant_profile(session, tenant_id)
    profile.profile_version += 1
    profile.status = "sample_insufficient"
    profile.style_summary = ""
    profile.topic_weights = {}
    profile.phrase_patterns = []
    profile.reply_patterns = []
    profile.comment_patterns = []
    profile.forbidden_learning = []
    profile.source_sample_count = 0
    version = TenantLearningProfileVersion(
        tenant_id=tenant_id,
        profile_version=profile.profile_version,
        profile_snapshot=_profile_snapshot(profile),
        source_snapshot={"cleared": True},
        quality_rule_version=(latest_quality_rule(session, tenant_id).rule_version if latest_quality_rule(session, tenant_id) else 0),
        sample_count=0,
        created_by=actor,
    )
    run = TenantLearningRun(tenant_id=tenant_id, run_type="clear", status="success", profile_version=profile.profile_version, quality_rule_version=version.quality_rule_version, trace_id=f"profile-clear-{profile.profile_version}")
    session.add_all([version, run])
    audit(session, tenant_id=tenant_id, actor=actor, action="清空全站目标画像", target_type="target_profile", target_id=str(tenant_id), detail=reason.strip())
    session.flush()
    return get_target_profile_overview(session, tenant_id)


def start_source_run(session: Session, tenant_id: int, source_id: str, run_type: str, *, actor: str) -> dict[str, Any]:
    if run_type not in {"sync", "pull_history"}:
        raise ValueError("学习运行类型不支持")
    source = session.get(TenantLearningSource, source_id)
    if not source or source.tenant_id != tenant_id:
        raise ValueError("学习来源不存在")
    from app.services.tenant_learning_samples import ingest_source_samples

    try:
        counts = ingest_source_samples(session, source, run_type)
    except Exception as exc:
        run = _record_failed_run(
            session,
            tenant_id,
            run_type=run_type,
            failure_detail=str(exc),
            trace_id=f"{run_type}-{source.id}-failed",
            source_id=source.id,
        )
        audit(session, tenant_id=tenant_id, actor=actor, action="执行目标画像学习同步失败" if run_type == "sync" else "执行目标画像历史拉取失败", target_type="target_profile_source", target_id=source.id, detail=run.failure_detail)
        raise TargetProfileRunFailed(run.failure_detail) from exc
    run = TenantLearningRun(
        tenant_id=tenant_id,
        source_id=source.id,
        run_type=run_type,
        status="success",
        sample_count=counts["sample_count"],
        accepted_count=counts["accepted_count"],
        rejected_count=counts["rejected_count"],
        trace_id=f"{run_type}-{source.id}",
    )
    session.add(run)
    audit(session, tenant_id=tenant_id, actor=actor, action="执行目标画像学习同步" if run_type == "sync" else "执行目标画像历史拉取", target_type="target_profile_source", target_id=source.id, detail=f"run_type={run_type}")
    session.flush()
    return _run_payload(run)


def list_runs(session: Session, tenant_id: int) -> dict[str, Any]:
    runs = session.scalars(select(TenantLearningRun).where(TenantLearningRun.tenant_id == tenant_id).order_by(TenantLearningRun.created_at.desc())).all()
    return {"items": [_run_payload(run) for run in runs], "total": len(runs)}


def ensure_tenant_profile(session: Session, tenant_id: int) -> TenantLearningProfile:
    profile = session.scalar(select(TenantLearningProfile).where(TenantLearningProfile.tenant_id == tenant_id))
    if profile:
        _ensure_initial_profile_version(session, tenant_id, profile)
        return profile
    profile = TenantLearningProfile(tenant_id=tenant_id, profile_version=0, status="sample_insufficient")
    session.add(profile)
    session.flush()
    _ensure_initial_profile_version(session, tenant_id, profile)
    return profile


def _ensure_initial_profile_version(session: Session, tenant_id: int, profile: TenantLearningProfile) -> None:
    if profile.profile_version != 0:
        return
    existing = session.scalar(
        select(TenantLearningProfileVersion).where(
            TenantLearningProfileVersion.tenant_id == tenant_id,
            TenantLearningProfileVersion.profile_version == 0,
        )
    )
    if existing:
        return
    session.add(
        TenantLearningProfileVersion(
            tenant_id=tenant_id,
            profile_version=0,
            profile_snapshot=_profile_snapshot(profile),
            source_snapshot={"initial_empty": True},
            sample_count=0,
            created_by="system",
        )
    )
    session.flush()


def ensure_quality_rule(session: Session, tenant_id: int) -> TenantLearningQualityRule:
    rule = latest_quality_rule(session, tenant_id)
    if rule:
        return rule
    rule = TenantLearningQualityRule(tenant_id=tenant_id, rule_version=1, **DEFAULT_RULES)
    session.add(rule)
    session.flush()
    return rule


def latest_quality_rule(session: Session, tenant_id: int) -> TenantLearningQualityRule | None:
    return session.scalar(select(TenantLearningQualityRule).where(TenantLearningQualityRule.tenant_id == tenant_id).order_by(TenantLearningQualityRule.rule_version.desc()).limit(1))


def _latest_quality_rule_version(session: Session, tenant_id: int) -> int:
    rule = latest_quality_rule(session, tenant_id)
    return rule.rule_version if rule else 0


def _record_failed_run(
    session: Session,
    tenant_id: int,
    *,
    run_type: str,
    failure_detail: str,
    trace_id: str,
    source_id: str = "",
    quality_rule_version: int = 0,
    profile_version: int = 0,
) -> TenantLearningRun:
    run = TenantLearningRun(
        tenant_id=tenant_id,
        source_id=source_id,
        run_type=run_type,
        status="failed",
        failure_detail=failure_detail,
        trace_id=trace_id,
        quality_rule_version=quality_rule_version,
        profile_version=profile_version,
    )
    session.add(run)
    session.flush()
    return run


def target_profile_usage(session: Session, tenant_id: int) -> dict[str, Any]:
    task_types = ["group_ai_chat", "channel_comment"]
    rows = session.execute(
        select(Task.type, func.count()).where(
            Task.tenant_id == tenant_id,
            Task.type.in_(task_types),
            Task.status.in_(["running", "draft", "paused"]),
            Task.deleted_at.is_(None),
        ).group_by(Task.type)
    ).all()
    distribution = {task_type: int(count) for task_type, count in rows}
    return {"running_task_count": sum(distribution.values()), "task_type_distribution": distribution, "recent_uses": []}


def _profile_preview_payload(profile: TenantLearningProfile, profile_scene: str, unavailable_reason: str) -> dict[str, Any]:
    return {
        "profile_id": profile.id,
        "profile_scene": profile_scene,
        "learning_enabled": profile.learning_enabled,
        "profile_version": profile.profile_version,
        "source_sample_count": profile.source_sample_count,
        "sample_sufficiency": "sufficient" if profile.source_sample_count > 0 else "insufficient",
        "profile_unavailable_reason": unavailable_reason,
        "profile_hit_summary": profile.style_summary if not unavailable_reason else "",
    }


def _unavailable_preview(profile_scene: str, reason: str) -> dict[str, Any]:
    return {
        "profile_id": "",
        "profile_scene": profile_scene,
        "learning_enabled": False,
        "profile_version": 0,
        "source_sample_count": 0,
        "sample_sufficiency": "missing",
        "profile_unavailable_reason": reason,
        "profile_hit_summary": "",
    }


def _quality_rule_payload(rule: TenantLearningQualityRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "tenant_id": rule.tenant_id,
        "rule_version": rule.rule_version,
        "identity_filters": rule.identity_filters or {},
        "text_filters": rule.text_filters or {},
        "template_filters": rule.template_filters or {},
        "scoring_thresholds": rule.scoring_thresholds or {},
        "scene_weights": rule.scene_weights or {},
        "forbidden_patterns": rule.forbidden_patterns or {},
        "updated_by": rule.updated_by,
        "updated_at": _iso(rule.updated_at),
    }


def _sample_payload(sample: TenantLearningSample) -> dict[str, Any]:
    return {
        "id": sample.id,
        "source_id": sample.source_id,
        "source_message_id": sample.source_message_id,
        "source_scene": sample.source_scene,
        "sender_name": sample.sender_name,
        "text": sample.text,
        "learning_status": sample.learning_status,
        "quality_score": sample.quality_score,
        "quality_rule_version": sample.quality_rule_version,
        "reject_reason": sample.reject_reason,
        "downweight_reason": sample.downweight_reason,
        "decision_by": sample.decision_by,
        "decision_at": _iso(sample.decision_at),
        "sent_at": _iso(sample.sent_at),
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
    }


def _profile_snapshot(profile: TenantLearningProfile) -> dict[str, Any]:
    return {
        "profile_version": profile.profile_version,
        "status": profile.status,
        "style_summary": profile.style_summary,
        "topic_weights": profile.topic_weights or {},
        "phrase_patterns": profile.phrase_patterns or [],
        "reply_patterns": profile.reply_patterns or [],
        "comment_patterns": profile.comment_patterns or [],
        "forbidden_learning": profile.forbidden_learning or [],
        "source_sample_count": profile.source_sample_count,
    }


def _build_style_summary(samples: list[TenantLearningSample]) -> str:
    if not samples:
        return ""
    snippets = [sample.text.strip() for sample in samples if sample.text.strip()]
    return "；".join(snippet[:80] for snippet in snippets[:5])


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None
