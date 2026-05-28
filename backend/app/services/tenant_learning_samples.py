from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChannelMessage,
    ChannelMessageComment,
    GroupContextMessage,
    OperationTarget,
    TenantLearningQualityRule,
    TenantLearningRun,
    TenantLearningSample,
    TenantLearningSource,
    TgGroup,
)
from app.services._common import _now
from app.services.tenant_target_profile import ensure_quality_rule


GROUP_CHAT_SCENE = "group_chat"
CHANNEL_COMMENT_SCENE = "channel_comment"
DISCUSSION_REPLY_SCENE = "discussion_reply"


def record_group_learning_sample(session: Session, group: TgGroup, snapshot: Any) -> TenantLearningSample | None:
    target = _target_for_peer(session, group.tenant_id, group.tg_peer_id, "group")
    if not target:
        return None
    source = _enabled_source_for_target(session, target)
    if not source:
        return None
    text = str(getattr(snapshot, "content", "") or "").strip()
    source_message_id = str(getattr(snapshot, "remote_message_id", "") or "")
    if not text or not source_message_id:
        return None
    source.last_sync_at = _now()
    return _upsert_sample(
        session,
        source,
        source_message_id=source_message_id,
        source_scene=GROUP_CHAT_SCENE,
        text=text,
        sender_peer_id=str(getattr(snapshot, "sender_peer_id", "") or ""),
        sender_username=str(getattr(snapshot, "sender_username", "") or "").lstrip("@"),
        sender_name=str(getattr(snapshot, "sender_name", "") or "真人用户"),
        is_bot=bool(getattr(snapshot, "is_bot", False)),
        sent_at=getattr(snapshot, "sent_at", None),
    )


def record_channel_comment_sample(session: Session, comment: ChannelMessageComment) -> TenantLearningSample | None:
    target = session.get(OperationTarget, comment.channel_target_id)
    if not target or target.tenant_id != comment.tenant_id:
        return None
    source = _enabled_source_for_target(session, target)
    if not source:
        return None
    source_scene = DISCUSSION_REPLY_SCENE if comment.parent_comment_message_id else CHANNEL_COMMENT_SCENE
    source.last_sync_at = _now()
    return _upsert_sample(
        session,
        source,
        source_message_id=f"{comment.channel_message_id}:{comment.comment_message_id}",
        source_scene=source_scene,
        text=str(comment.content_preview or "").strip(),
        sender_peer_id=comment.author_peer_id,
        sender_username=comment.author_username,
        sender_name=comment.author_name,
        is_bot=comment.is_bot,
        sent_at=comment.published_at,
    )


def ingest_source_samples(session: Session, source: TenantLearningSource, run_type: str) -> dict[str, int]:
    target = session.get(OperationTarget, source.target_id)
    if not target or target.tenant_id != source.tenant_id:
        raise ValueError("学习来源目标不存在")
    if not source.is_enabled:
        raise ValueError("学习来源已停用")
    limit = 500 if run_type == "pull_history" else 100
    if target.target_type == "channel":
        samples = _ingest_channel_comments(session, source, limit)
    else:
        samples = _ingest_group_messages(session, source, target, limit)
    source.last_history_pull_at = _now() if run_type == "pull_history" else source.last_history_pull_at
    source.last_sync_at = _now() if run_type == "sync" else source.last_sync_at
    source.source_status = "active"
    source.last_failure_detail = ""
    return _sample_counts(samples)


def recompute_source_candidates(session: Session, tenant_id: int) -> dict[str, int]:
    samples = session.scalars(select(TenantLearningSample).where(TenantLearningSample.tenant_id == tenant_id)).all()
    recomputed: list[TenantLearningSample] = []
    for sample in samples:
        if sample.decision_by:
            continue
        status, score, reason, rule = _classify_sample(
            session,
            tenant_id,
            text=sample.text,
            sender_username=sample.sender_username,
            sender_name=sample.sender_name,
            is_bot=sample.is_bot,
        )
        _apply_decision(sample, status, score, reason, rule.rule_version)
        recomputed.append(sample)
    return _sample_counts(recomputed)


def _ingest_group_messages(session: Session, source: TenantLearningSource, target: OperationTarget, limit: int) -> list[TenantLearningSample]:
    group = session.scalar(select(TgGroup).where(TgGroup.tenant_id == target.tenant_id, TgGroup.tg_peer_id == target.tg_peer_id))
    if not group:
        return []
    rows = session.scalars(
        select(GroupContextMessage)
        .where(GroupContextMessage.tenant_id == target.tenant_id, GroupContextMessage.group_id == group.id)
        .order_by(GroupContextMessage.sent_at.desc(), GroupContextMessage.id.desc())
        .limit(limit)
    ).all()
    samples: list[TenantLearningSample] = []
    for row in rows:
        sample = _upsert_sample(
            session,
            source,
            source_message_id=str(row.remote_message_id),
            source_scene=GROUP_CHAT_SCENE,
            text=row.content,
            sender_peer_id=row.sender_peer_id,
            sender_username=row.sender_username,
            sender_name=row.sender_name,
            is_bot=row.is_bot,
            sent_at=row.sent_at,
        )
        if sample:
            samples.append(sample)
    return samples


def _ingest_channel_comments(session: Session, source: TenantLearningSource, limit: int) -> list[TenantLearningSample]:
    rows = session.scalars(
        select(ChannelMessageComment)
        .where(ChannelMessageComment.tenant_id == source.tenant_id, ChannelMessageComment.channel_target_id == source.target_id)
        .order_by(ChannelMessageComment.published_at.desc(), ChannelMessageComment.id.desc())
        .limit(limit)
    ).all()
    samples = [record_channel_comment_sample(session, row) for row in rows]
    return [sample for sample in samples if sample]


def _upsert_sample(
    session: Session,
    source: TenantLearningSource,
    *,
    source_message_id: str,
    source_scene: str,
    text: str,
    sender_peer_id: str,
    sender_username: str,
    sender_name: str,
    is_bot: bool,
    sent_at: Any,
) -> TenantLearningSample | None:
    text = text.strip()
    if not text or not source_message_id:
        return None
    rule_status, score, reason, rule = _classify_sample(
        session,
        source.tenant_id,
        text=text,
        sender_username=sender_username,
        sender_name=sender_name,
        is_bot=is_bot,
    )
    sample = session.scalar(
        select(TenantLearningSample).where(
            TenantLearningSample.tenant_id == source.tenant_id,
            TenantLearningSample.source_id == source.id,
            TenantLearningSample.source_message_id == source_message_id,
        )
    )
    if not sample:
        sample = TenantLearningSample(tenant_id=source.tenant_id, source_id=source.id, source_message_id=source_message_id)
        session.add(sample)
    sample.source_scene = source_scene
    sample.sender_peer_id = sender_peer_id
    sample.sender_username = sender_username
    sample.sender_name = sender_name
    sample.is_bot = is_bot
    sample.raw_text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    sample.text = text[:4000]
    sample.sent_at = sent_at
    _apply_decision(sample, rule_status, score, reason, rule.rule_version)
    session.flush()
    return sample


def _classify_sample(
    session: Session,
    tenant_id: int,
    *,
    text: str,
    sender_username: str,
    sender_name: str,
    is_bot: bool,
) -> tuple[str, int, str, TenantLearningQualityRule]:
    rule = ensure_quality_rule(session, tenant_id)
    identity = rule.identity_filters or {}
    text_filters = rule.text_filters or {}
    forbidden = rule.forbidden_patterns or {}
    if identity.get("exclude_bots", True) and is_bot:
        return "rejected", 0, "bot_sender", rule
    if identity.get("exclude_managed_accounts", True) and _looks_managed(sender_username, sender_name):
        return "rejected", 0, "managed_account", rule
    reason = _text_reject_reason(text, text_filters, forbidden)
    if reason:
        return "rejected", 0, reason, rule
    if _looks_template(text, rule.template_filters or {}):
        return "downweighted", 40, "template_like", rule
    return "accepted", 100, "", rule


def _text_reject_reason(text: str, text_filters: dict[str, Any], forbidden: dict[str, Any]) -> str:
    if len(text) < int(text_filters.get("min_length") or 0):
        return "too_short"
    max_length = int(text_filters.get("max_length") or 4000)
    if len(text) > max_length:
        return "too_long"
    keywords = [str(item).strip() for item in text_filters.get("keywords") or [] if str(item).strip()]
    if keywords and not any(keyword in text for keyword in keywords):
        return "keyword_mismatch"
    forbidden_keywords = [str(item).strip() for item in forbidden.get("keywords") or [] if str(item).strip()]
    if any(keyword in text for keyword in forbidden_keywords):
        return "forbidden_keyword"
    if forbidden.get("links", True) and re.search(r"https?://|t\.me/|www\.", text, re.I):
        return "contains_link"
    if forbidden.get("contacts", True) and re.search(r"\b1[3-9]\d{9}\b|@\w{4,}", text):
        return "contains_contact"
    return ""


def _looks_template(text: str, template_filters: dict[str, Any]) -> bool:
    phrases = [str(item).strip() for item in template_filters.get("phrases") or [] if str(item).strip()]
    return any(phrase in text for phrase in phrases)


def _looks_managed(username: str, sender_name: str) -> bool:
    identity = f"{username} {sender_name}".lower()
    return any(marker in identity for marker in ["bot", "admin", "客服", "小助理"])


def _apply_decision(sample: TenantLearningSample, status: str, score: int, reason: str, rule_version: int) -> None:
    sample.learning_status = status
    sample.quality_score = score
    sample.quality_rule_version = rule_version
    sample.reject_reason = reason if status == "rejected" else ""
    sample.downweight_reason = reason if status == "downweighted" else ""


def _enabled_source_for_target(session: Session, target: OperationTarget) -> TenantLearningSource | None:
    return session.scalar(
        select(TenantLearningSource).where(
            TenantLearningSource.tenant_id == target.tenant_id,
            TenantLearningSource.target_id == target.id,
            TenantLearningSource.is_enabled.is_(True),
        )
    )


def _target_for_peer(session: Session, tenant_id: int, peer_id: str, target_type: str) -> OperationTarget | None:
    return session.scalar(select(OperationTarget).where(OperationTarget.tenant_id == tenant_id, OperationTarget.tg_peer_id == peer_id, OperationTarget.target_type == target_type))


def _sample_counts(samples: list[TenantLearningSample]) -> dict[str, int]:
    return {
        "sample_count": len(samples),
        "accepted_count": sum(1 for sample in samples if sample.learning_status == "accepted"),
        "rejected_count": sum(1 for sample in samples if sample.learning_status == "rejected"),
    }
