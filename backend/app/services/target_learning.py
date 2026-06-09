from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AuditLog, ChannelMessageComment, OperationTarget, TargetLearningProfile, TargetLearningProfileVersion, TargetLearningSample, TgAccount, TgGroup, TgGroupAccount
from app.services._common import _now, audit
from app.services.content_filters import contains_coarse_language
from app.services.target_learning_query import page_number, page_size, sample_time_filter
from app.services.target_learning_samples import refresh_comment_sample
from app.services.target_learning_versions import record_profile_version_snapshot


GROUP_CHAT_SCENE = "group_chat"
CHANNEL_COMMENT_SCENE = "channel_comment"
DISCUSSION_REPLY_SCENE = "discussion_reply"
LEARNING_STATUSES = {"candidate", "accepted", "rejected", "downweighted", "applied"}
PROFILE_SCENES = (GROUP_CHAT_SCENE, CHANNEL_COMMENT_SCENE, DISCUSSION_REPLY_SCENE)


def operation_target_for_group(session: Session, group: TgGroup) -> OperationTarget | None:
    return session.scalar(
        select(OperationTarget)
        .where(OperationTarget.tenant_id == group.tenant_id, OperationTarget.tg_peer_id == group.tg_peer_id)
        .order_by(OperationTarget.id.asc())
        .limit(1)
    )


def record_group_learning_sample(session: Session, group: TgGroup, snapshot: Any, *, profile_scene: str = GROUP_CHAT_SCENE) -> TargetLearningSample | None:
    target = operation_target_for_group(session, group)
    if not target:
        return None
    message_id = str(getattr(snapshot, "remote_message_id", "") or "")
    if not message_id or _existing_sample(session, target.id, profile_scene, message_id):
        return None
    profile = _ensure_profile(session, target.tenant_id, target.id, profile_scene)
    status, reject_reason, downweight_reason, quality_score = _classify_group_snapshot(session, group, snapshot)
    if not profile.learning_enabled:
        status, reject_reason, downweight_reason, quality_score = "rejected", "learning_disabled", "", 0
    sample = _new_sample(group, target.id, snapshot, profile_scene, status, reject_reason, downweight_reason, quality_score)
    try:
        with session.begin_nested():
            session.add(sample)
            session.flush()
    except IntegrityError:
        return None
    if status == "accepted":
        rebuild_learning_profile(session, target.id, profile_scene, actor="监听学习服务")
    return sample


def record_channel_comment_learning_sample(session: Session, comment: ChannelMessageComment) -> TargetLearningSample | None:
    target = _require_target(session, comment.tenant_id, comment.channel_target_id)
    message_id = str(comment.comment_message_id or "")
    if not message_id:
        return None
    profile = _ensure_profile(session, target.tenant_id, target.id, CHANNEL_COMMENT_SCENE)
    status, reject_reason, downweight_reason, quality_score = _classify_comment(session, comment)
    if not profile.learning_enabled:
        status, reject_reason, downweight_reason, quality_score = "rejected", "learning_disabled", "", 0
    existing_id = _existing_sample(session, target.id, CHANNEL_COMMENT_SCENE, message_id)
    if existing_id:
        sample = session.get(TargetLearningSample, existing_id)
        if not sample:
            return None
        previous_status = sample.learning_status
        refresh_comment_sample(sample, comment, status, reject_reason, downweight_reason, quality_score)
        session.flush()
        if previous_status != status or status == "accepted":
            rebuild_learning_profile(session, target.id, CHANNEL_COMMENT_SCENE, actor="监听学习服务")
        return sample
    sample = TargetLearningSample(
        tenant_id=comment.tenant_id,
        target_id=target.id,
        source_message_id=message_id,
        source_scene="channel_comment",
        profile_scene=CHANNEL_COMMENT_SCENE,
        sender_peer_id=comment.author_peer_id or "",
        sender_username=str(getattr(comment, "author_username", "") or "").lstrip("@"),
        sender_name=comment.author_name or "",
        is_bot=bool(getattr(comment, "is_bot", False)),
        is_managed_account=status == "rejected" and reject_reason == "managed_account",
        message_type="comment",
        text=(comment.content_preview or "")[:4000],
        learning_status=status,
        reject_reason=reject_reason,
        downweight_reason=downweight_reason,
        quality_score=quality_score,
        observed_reply_count=int(comment.reply_count or 0),
        sent_at=comment.published_at,
    )
    session.add(sample)
    session.flush()
    if status == "accepted":
        rebuild_learning_profile(session, target.id, CHANNEL_COMMENT_SCENE, actor="监听学习服务")
    return sample


def get_learning_profile_payload(session: Session, tenant_id: int, target_id: int) -> dict[str, Any]:
    _require_target(session, tenant_id, target_id)
    profiles = session.scalars(select(TargetLearningProfile).where(TargetLearningProfile.tenant_id == tenant_id, TargetLearningProfile.target_id == target_id))
    profile_map = {profile.profile_scene: profile for profile in profiles}
    return {
        "target_id": target_id,
        "profiles": [_profile_payload(session, profile_map[scene]) if scene in profile_map else _unavailable_preview(scene, "profile_missing") for scene in PROFILE_SCENES],
    }


def list_learning_samples_payload(session: Session, tenant_id: int, target_id: int, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    _require_target(session, tenant_id, target_id)
    stmt = select(TargetLearningSample).where(TargetLearningSample.tenant_id == tenant_id, TargetLearningSample.target_id == target_id)
    filters = filters or {}
    if filters.get("profile_scene"):
        stmt = stmt.where(TargetLearningSample.profile_scene == str(filters["profile_scene"]))
    if filters.get("learning_status"):
        stmt = stmt.where(TargetLearningSample.learning_status == str(filters["learning_status"]))
    if filters.get("reject_reason"):
        stmt = stmt.where(TargetLearningSample.reject_reason == str(filters["reject_reason"]))
    if filters.get("downweight_reason"):
        stmt = stmt.where(TargetLearningSample.downweight_reason == str(filters["downweight_reason"]))
    stmt = sample_time_filter(stmt, filters)
    total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    size = page_size(filters)
    page = page_number(filters)
    samples = session.scalars(stmt.order_by(TargetLearningSample.created_at.desc()).offset((page - 1) * size).limit(size)).all()
    return {"target_id": target_id, "page": page, "page_size": size, "total": total, "items": [_sample_payload(sample) for sample in samples]}


def rebuild_learning_profile(session: Session, target_id: int, profile_scene: str, *, actor: str = "system", reason: str = "") -> TargetLearningProfile:
    if actor != "监听学习服务" and not reason.strip():
        raise ValueError("请填写重建原因")
    target = session.get(OperationTarget, target_id)
    if not target:
        raise ValueError("目标不存在")
    profile = _ensure_profile(session, target.tenant_id, target_id, profile_scene)
    accepted = _accepted_samples(session, target.tenant_id, target_id, profile_scene)
    _apply_profile_summary(profile, accepted)
    for sample in accepted:
        sample.applied_profile_version = profile.profile_version
    session.flush()
    _add_profile_version(session, profile, accepted, actor)
    if reason:
        audit(session, tenant_id=target.tenant_id, actor=actor, action="重建目标画像", target_type="operation_target", target_id=str(target_id), detail=f"profile_scene={profile_scene}; reason={reason}")
    return profile


def set_learning_enabled(session: Session, tenant_id: int, target_id: int, profile_scene: str, enabled: bool, *, actor: str, reason: str) -> TargetLearningProfile:
    if not reason.strip():
        raise ValueError("请填写操作原因")
    _require_target(session, tenant_id, target_id)
    profile = _ensure_profile(session, tenant_id, target_id, profile_scene)
    profile.learning_enabled = enabled
    profile.disabled_reason = "" if enabled else reason.strip()
    audit(session, tenant_id=tenant_id, actor=actor, action="恢复目标画像学习" if enabled else "禁用目标画像学习", target_type="operation_target", target_id=str(target_id), detail=f"profile_scene={profile_scene}; reason={reason.strip()}")
    return profile


def clear_learning_profile(session: Session, tenant_id: int, target_id: int, profile_scene: str, *, actor: str, reason: str) -> TargetLearningProfile:
    if not reason.strip():
        raise ValueError("请填写清空原因")
    _require_target(session, tenant_id, target_id)
    profile = _ensure_profile(session, tenant_id, target_id, profile_scene)
    _clear_profile(profile)
    record_profile_version_snapshot(session, profile, actor, {"cleared": True})
    audit(session, tenant_id=tenant_id, actor=actor, action="清空目标画像", target_type="operation_target", target_id=str(target_id), detail=f"profile_scene={profile_scene}; reason={reason.strip()}")
    return profile


def update_learning_sample_status(session: Session, tenant_id: int, sample_id: str, status: str, *, actor: str, reason: str) -> TargetLearningSample:
    if status not in LEARNING_STATUSES:
        raise ValueError("学习样本状态不支持")
    if not reason.strip():
        raise ValueError("请填写调整原因")
    sample = session.get(TargetLearningSample, sample_id)
    if not sample or sample.tenant_id != tenant_id:
        raise ValueError("学习样本不存在")
    sample.learning_status = status
    sample.reject_reason = reason.strip() if status == "rejected" else sample.reject_reason
    sample.downweight_reason = reason.strip() if status == "downweighted" else sample.downweight_reason
    if status != "accepted":
        sample.applied_profile_version = None
    sample.status_updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="调整学习样本状态", target_type="target_learning_sample", target_id=sample.id, detail=f"status={status}; reason={reason.strip()}")
    rebuild_learning_profile(session, sample.target_id, sample.profile_scene, actor="监听学习服务")
    return sample


def learning_profile_preview(session: Session, tenant_id: int, target_id: int | None, profile_scene: str) -> dict[str, Any]:
    if not target_id:
        return _unavailable_preview(profile_scene, "no_target")
    profile = _profile(session, tenant_id, target_id, profile_scene)
    if not profile:
        return _unavailable_preview(profile_scene, "profile_missing")
    if not profile.learning_enabled:
        return _profile_preview(profile, "learning_disabled")
    if profile.source_sample_count <= 0:
        return _profile_preview(profile, "sample_insufficient")
    return _profile_preview(profile, "")


def _classify_group_snapshot(session: Session, group: TgGroup, snapshot: Any) -> tuple[str, str, str, int]:
    if bool(getattr(snapshot, "is_bot", False)):
        return "rejected", "bot", "", 0
    if _is_source_identity(group, snapshot):
        return "rejected", "source_self_or_channel", "", 0
    if _is_managed_sender(session, group, snapshot):
        return "rejected", "managed_account", "", 0
    text = _snapshot_text(snapshot)
    if contains_coarse_language(text):
        return "rejected", "coarse_language", "", 0
    if _looks_like_system_or_button(text):
        return "rejected", "system_or_button_prompt", "", 0
    if str(getattr(snapshot, "message_type", "") or "text") != "text" and _looks_like_repeated_ad(text):
        return "downweighted", "", "duplicate_ad_caption", 20
    if _looks_like_template(text):
        return "downweighted", "", "template_text", 40
    return "accepted", "", "", 100


def _classify_comment(session: Session, comment: ChannelMessageComment) -> tuple[str, str, str, int]:
    if bool(getattr(comment, "is_bot", False)):
        return "rejected", "bot", "", 0
    if _is_managed_comment_author(session, comment):
        return "rejected", "managed_account", "", 0
    text = str(comment.content_preview or "").strip()
    if not text:
        return "rejected", "empty_comment", "", 0
    if contains_coarse_language(text):
        return "rejected", "coarse_language", "", 0
    if _looks_like_system_or_button(text):
        return "rejected", "system_or_button_prompt", "", 0
    if _looks_like_repeated_ad(text):
        return "downweighted", "", "duplicate_ad_caption", 20
    if _looks_like_template(text):
        return "downweighted", "", "template_text", 40
    return "accepted", "", "", 100


def _is_managed_comment_author(session: Session, comment: ChannelMessageComment) -> bool:
    sender_values = {
        str(comment.author_peer_id or "").lower(),
        str(getattr(comment, "author_username", "") or "").lower().lstrip("@"),
        str(comment.author_name or "").lower(),
    }
    if not any(sender_values):
        return False
    accounts = session.scalars(select(TgAccount).where(TgAccount.tenant_id == comment.tenant_id, TgAccount.deleted_at.is_(None)))
    for account in accounts:
        keys = {str(account.id), f"account:{account.id}", str(account.display_name or "").lower()}
        if account.username:
            keys.add(account.username.lower().lstrip("@"))
        if keys & sender_values:
            return True
    return False


def _new_sample(group: TgGroup, target_id: int, snapshot: Any, profile_scene: str, status: str, reject: str, downweight: str, score: int) -> TargetLearningSample:
    return TargetLearningSample(
        tenant_id=group.tenant_id,
        target_id=target_id,
        source_message_id=str(getattr(snapshot, "remote_message_id", "") or ""),
        source_scene="listener",
        profile_scene=profile_scene,
        sender_peer_id=str(getattr(snapshot, "sender_peer_id", "") or ""),
        sender_username=str(getattr(snapshot, "sender_username", "") or "").lstrip("@"),
        sender_name=str(getattr(snapshot, "sender_name", "") or ""),
        is_bot=bool(getattr(snapshot, "is_bot", False)),
        is_managed_account=status == "rejected" and reject == "managed_account",
        message_type=str(getattr(snapshot, "message_type", "") or "text"),
        text=str(getattr(snapshot, "content", "") or "")[:4000],
        caption=str(getattr(snapshot, "caption", "") or "")[:4000],
        learning_status=status,
        reject_reason=reject,
        downweight_reason=downweight,
        quality_score=score,
        sent_at=getattr(snapshot, "sent_at", None),
    )


def _apply_profile_summary(profile: TargetLearningProfile, samples: list[TargetLearningSample]) -> None:
    texts = [_sample_text(sample) for sample in samples if _sample_text(sample)]
    profile.source_sample_count = len(samples)
    profile.profile_version = int(profile.profile_version or 0) + 1
    profile.style_summary = _style_summary(texts)
    profile.phrase_patterns = texts[:8]
    profile.topic_weights = _topic_weights(texts)
    profile.reply_patterns = _reply_patterns(texts)
    profile.last_rebuilt_at = _now()


def _add_profile_version(session: Session, profile: TargetLearningProfile, samples: list[TargetLearningSample], actor: str) -> None:
    sample_times = [_naive_datetime(sample.sent_at) for sample in samples if sample.sent_at]
    version = TargetLearningProfileVersion(
        tenant_id=profile.tenant_id,
        profile_id=profile.id,
        profile_version=profile.profile_version,
        source_sample_count=len(samples),
        sample_window_start=min(sample_times, default=None),
        sample_window_end=max(sample_times, default=None),
        summary_snapshot=_profile_summary_snapshot(profile),
        quality_snapshot={"accepted": len(samples)},
        created_by=actor,
    )
    session.add(version)


def _ensure_profile(session: Session, tenant_id: int, target_id: int, profile_scene: str) -> TargetLearningProfile:
    profile = _profile(session, tenant_id, target_id, profile_scene)
    if profile:
        return profile
    profile = TargetLearningProfile(tenant_id=tenant_id, target_id=target_id, profile_scene=profile_scene)
    session.add(profile)
    session.flush()
    return profile


def _profile(session: Session, tenant_id: int, target_id: int, profile_scene: str) -> TargetLearningProfile | None:
    return session.scalar(
        select(TargetLearningProfile).where(TargetLearningProfile.tenant_id == tenant_id, TargetLearningProfile.target_id == target_id, TargetLearningProfile.profile_scene == profile_scene)
    )


def _existing_sample(session: Session, target_id: int, profile_scene: str, message_id: str) -> str | None:
    return session.scalar(
        select(TargetLearningSample.id).where(TargetLearningSample.target_id == target_id, TargetLearningSample.profile_scene == profile_scene, TargetLearningSample.source_message_id == message_id)
    )


def _accepted_samples(session: Session, tenant_id: int, target_id: int, profile_scene: str) -> list[TargetLearningSample]:
    return list(
        session.scalars(
            select(TargetLearningSample)
            .where(TargetLearningSample.tenant_id == tenant_id, TargetLearningSample.target_id == target_id, TargetLearningSample.profile_scene == profile_scene, TargetLearningSample.learning_status == "accepted")
            .order_by(TargetLearningSample.sent_at.desc().nullslast(), TargetLearningSample.created_at.desc())
            .limit(200)
        )
    )


def _is_managed_sender(session: Session, group: TgGroup, snapshot: Any) -> bool:
    keys = _managed_sender_keys(session, group)
    sender_values = {
        str(getattr(snapshot, "sender_peer_id", "") or "").lower(),
        str(getattr(snapshot, "sender_name", "") or "").lower(),
        str(getattr(snapshot, "sender_username", "") or "").lower().lstrip("@"),
    }
    return bool(keys & sender_values)


def _is_source_identity(group: TgGroup, snapshot: Any) -> bool:
    sender_peer_id = str(getattr(snapshot, "sender_peer_id", "") or "")
    sender_username = str(getattr(snapshot, "sender_username", "") or "").lower().lstrip("@")
    group_username = str(getattr(group, "username", "") or "").lower().lstrip("@")
    return sender_peer_id == group.tg_peer_id or bool(group_username and sender_username == group_username)


def _managed_sender_keys(session: Session, group: TgGroup) -> set[str]:
    accounts = session.scalars(select(TgAccount).join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id).where(TgGroupAccount.group_id == group.id, TgAccount.deleted_at.is_(None)))
    keys: set[str] = set()
    for account in accounts:
        keys.update({str(account.id), f"account:{account.id}", (account.display_name or "").lower()})
        if account.username:
            keys.add(account.username.lower().lstrip("@"))
    return {key for key in keys if key}


def _snapshot_text(snapshot: Any) -> str:
    return str(getattr(snapshot, "caption", "") or getattr(snapshot, "content", "") or "").strip()


def _naive_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


def _sample_text(sample: TargetLearningSample) -> str:
    return str(sample.text or sample.caption or "").strip()


def _looks_like_system_or_button(text: str) -> bool:
    return any(keyword in text for keyword in ("自动公告", "点击按钮", "系统提示", "风控提示", "验证码", "加入频道"))


def _looks_like_repeated_ad(text: str) -> bool:
    return any(keyword in text for keyword in ("精品必吃榜", "踩坑包赔", "招商", "推广", "广告"))


def _looks_like_template(text: str) -> bool:
    generic = {"这个内容挺有参考价值", "这个角度不错", "说得比较实在", "支持一下"}
    return text in generic or bool(re.fullmatch(r"(.{2,6})\1{2,}", text))


def _style_summary(texts: list[str]) -> str:
    if not texts:
        return ""
    avg_len = round(sum(len(text) for text in texts) / len(texts), 1)
    return f"真人样本 {len(texts)} 条，平均短句长度 {avg_len}，偏向短句接话和具体追问。"


def _topic_weights(texts: list[str]) -> dict[str, int]:
    words: dict[str, int] = {}
    for text in texts:
        for token in re.findall(r"[\u4e00-\u9fff]{2,6}|[A-Za-z0-9_]{2,}", text):
            words[token] = words.get(token, 0) + 1
    return dict(sorted(words.items(), key=lambda item: item[1], reverse=True)[:20])


def _reply_patterns(texts: list[str]) -> list[str]:
    return [text for text in texts if "?" in text or "？" in text][:5]


def _clear_profile(profile: TargetLearningProfile) -> None:
    profile.style_summary = ""
    profile.topic_weights = {}
    profile.phrase_patterns = []
    profile.reply_patterns = []
    profile.comment_patterns = []
    profile.slang_terms = []
    profile.forbidden_learning = []
    profile.active_windows = []
    profile.profile_version = int(profile.profile_version or 0) + 1
    profile.source_sample_count = 0
    profile.last_rebuilt_at = _now()


def _profile_payload(session: Session, profile: TargetLearningProfile) -> dict[str, Any]:
    latest_use = _latest_ai_profile_use(session, profile)
    payload = _profile_summary_snapshot(profile)
    payload.update({"id": profile.id, "learning_enabled": profile.learning_enabled, "disabled_reason": profile.disabled_reason, "latest_ai_use": latest_use})
    return payload


def _profile_summary_snapshot(profile: TargetLearningProfile) -> dict[str, Any]:
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


def _sample_payload(sample: TargetLearningSample) -> dict[str, Any]:
    return {
        "id": sample.id,
        "profile_scene": sample.profile_scene,
        "source_message_id": sample.source_message_id,
        "sender_name": sample.sender_name,
        "message_type": sample.message_type,
        "text": sample.text,
        "caption": sample.caption,
        "learning_status": sample.learning_status,
        "reject_reason": sample.reject_reason,
        "downweight_reason": sample.downweight_reason,
        "quality_score": sample.quality_score,
        "sent_at": sample.sent_at.isoformat() if sample.sent_at else "",
    }


def _profile_preview(profile: TargetLearningProfile, unavailable_reason: str) -> dict[str, Any]:
    return {
        "profile_id": profile.id,
        "profile_scene": profile.profile_scene,
        "learning_enabled": profile.learning_enabled,
        "profile_version": profile.profile_version,
        "source_sample_count": profile.source_sample_count,
        "sample_sufficiency": "sufficient" if profile.source_sample_count > 0 else "insufficient",
        "last_rebuilt_at": profile.last_rebuilt_at.isoformat() if profile.last_rebuilt_at else "",
        "disabled_reason": profile.disabled_reason,
        "profile_unavailable_reason": unavailable_reason,
        "profile_hit_summary": profile.style_summary,
    }


def _unavailable_preview(profile_scene: str, reason: str) -> dict[str, Any]:
    return {"profile_id": "", "profile_scene": profile_scene, "learning_enabled": False, "profile_version": 0, "source_sample_count": 0, "sample_sufficiency": "missing", "profile_unavailable_reason": reason, "profile_hit_summary": ""}


def _latest_ai_profile_use(session: Session, profile: TargetLearningProfile) -> dict[str, Any]:
    row = session.scalar(
        select(AuditLog)
        .where(AuditLog.tenant_id == profile.tenant_id, AuditLog.target_type == "target_learning_profile", AuditLog.target_id == profile.id)
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    )
    return {"used_at": row.created_at.isoformat(), "detail": row.detail} if row else {}


def _require_target(session: Session, tenant_id: int, target_id: int) -> OperationTarget:
    target = session.get(OperationTarget, target_id)
    if not target or target.tenant_id != tenant_id:
        raise ValueError("运营目标不存在")
    return target
