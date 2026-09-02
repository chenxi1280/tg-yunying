from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Action, ChannelCommentGroundingAssignment, ChannelCommentGroundingSnapshot, ChannelMessage, ChannelMessageComment, OperationTarget, RuleSetVersion, TgGroup
from app.services.content_filters import filter_outbound_content
from app.services.rule_engine import OutputPolicyResult, apply_output_policy

from .ai_generator import clean_channel_comment_contents
from .channel_payloads import PostCommentPayload
from .channel_comment_grounding_evaluation import evaluate_grounding_claims
from .channel_comment_style_assignment import frozen_comment_style, length_matches_style


FIXED_RULE_SNAPSHOT_STATUSES = frozenset({"published", "archived"})
OPEN_COMMENT_HISTORY_STATUSES = (
    "pending",
    "claiming",
    "executing",
    "success",
    "unknown_after_send",
)


@dataclass(frozen=True)
class CommentQualityDecision:
    allowed: bool
    content: str
    code: str = ""
    detail: str = ""
    audit: dict | None = None


def evaluate_comment_generation_quality(
    session: Session,
    action: Action,
    *,
    payload: PostCommentPayload,
    content: str,
) -> CommentQualityDecision:
    if not _lock_channel_message(session, action, payload):
        return _unavailable_comment_decision()
    grounding_error = _grounding_binding_error(session, action, payload)
    if grounding_error is not None:
        return grounding_error
    version, error = _fixed_rule_version(session, action, payload)
    if error:
        return error
    return _evaluate_normal_candidate(
        session, action, version, payload=payload, content=content,
    )


def _evaluate_normal_candidate(
    session: Session,
    action: Action,
    version: RuleSetVersion,
    *,
    payload: PostCommentPayload,
    content: str,
) -> CommentQualityDecision:
    policy = apply_output_policy(
        content,
        version.output_checks or {},
        version.transforms or {},
    )
    audit = _rule_policy_audit(version, policy)
    if not policy.allowed:
        return CommentQualityDecision(
            False,
            "",
            "rule_output_rejected",
            policy.reason or "固定规则版本拒绝评论输出",
            audit,
        )
    previous = _same_message_comment_history(session, action, payload)
    cleaned = clean_channel_comment_contents([policy.content], previous, limit=1)
    if not cleaned:
        return CommentQualityDecision(
            False,
            "",
            "duplicate_rejected",
            "评论与同频道消息已有评论语义重复",
            audit,
        )
    final_content = str(cleaned[0])
    style_error = _style_error(payload, final_content, audit=audit)
    if style_error is not None:
        return style_error
    return _grounded_outbound_decision(
        session, action, payload=payload, content=final_content, audit=audit,
    )


def _grounded_outbound_decision(
    session: Session,
    action: Action,
    *,
    payload: PostCommentPayload,
    content: str,
    audit: dict,
) -> CommentQualityDecision:
    claim_decision = evaluate_grounding_claims(
        session, action, payload, content=content,
    )
    audit.update(_claim_audit(claim_decision.claim_results))
    if not claim_decision.allowed:
        return CommentQualityDecision(
            False, "", claim_decision.code, claim_decision.detail, audit,
        )
    return _outbound_decision(
        session,
        action,
        payload=payload,
        content=content,
        audit=audit,
    )


def _claim_audit(claim_results: tuple[dict, ...]) -> dict:
    return {
        "quality_contract_version": "channel_comment_grounding_quality_v1",
        "deterministic_claim_results": list(claim_results),
    }


def _style_error(
    payload: PostCommentPayload,
    content: str,
    *,
    audit: dict,
) -> CommentQualityDecision | None:
    if not payload.grounding_enrollment_id:
        return None
    assignment = frozen_comment_style(
        payload.grounding_snapshot_id, payload.target_ordinal,
    )
    audit["frozen_length_tier"] = assignment.length_tier
    audit["frozen_persona_key"] = assignment.persona_key
    if length_matches_style(content, assignment):
        return None
    return CommentQualityDecision(
        False,
        "",
        "comment_length_tier_mismatch",
        (
            f"评论未满足冻结字数层 {assignment.length_tier}: "
            f"{assignment.minimum_length}-{assignment.maximum_length}"
        ),
        audit,
    )


def _rule_policy_audit(
    version: RuleSetVersion,
    policy: OutputPolicyResult,
) -> dict:
    return {
        "rule_set_version_id": version.id,
        "rule_output_action": policy.action,
        "rule_output_transformed": policy.transformed,
        "rule_output_hits": list(policy.hits),
    }


def _grounding_binding_error(
    session: Session,
    action: Action,
    payload: PostCommentPayload,
) -> CommentQualityDecision | None:
    if not payload.source_revision_id:
        return None
    assignment = session.get(
        ChannelCommentGroundingAssignment,
        payload.grounding_assignment_id,
    ) if payload.grounding_assignment_id else None
    evidence_hash = hashlib.sha256(payload.message_content.encode("utf-8")).hexdigest()
    snapshot = session.get(
        ChannelCommentGroundingSnapshot, payload.grounding_snapshot_id,
    ) if payload.grounding_snapshot_id else None
    valid = bool(
        assignment
        and assignment.tenant_id == action.tenant_id
        and assignment.source_revision_id == payload.source_revision_id
        and assignment.quality_target_revision_id == payload.quality_target_revision_id
        and assignment.target_ordinal == payload.target_ordinal
        and assignment.evidence_hash == payload.grounding_evidence_hash == evidence_hash
        and assignment.assignment_state == "active"
        and snapshot
        and snapshot.id == assignment.grounding_snapshot_id
        and snapshot.source_revision_id == assignment.source_revision_id
        and snapshot.source_content_hash == evidence_hash
        and assignment.comment_grounding_revision == payload.comment_grounding_revision
        and assignment.teacher_candidate_id == payload.grounding_teacher_candidate_id
        and assignment.primary_evidence_id == payload.grounding_primary_evidence_id
    )
    if valid:
        return None
    return CommentQualityDecision(
        False,
        "",
        "grounding_assignment_invalid",
        "评论内容依据与冻结的来源修订或槽位分配不一致",
        {"grounding_assignment_id": payload.grounding_assignment_id},
    )


def evaluate_comment_fallback_quality(
    session: Session,
    action: Action,
    *,
    payload: PostCommentPayload,
    content: str,
) -> CommentQualityDecision:
    if not _lock_channel_message(session, action, payload):
        return _unavailable_comment_decision()
    version, error = _fixed_rule_version(session, action, payload)
    if error:
        return error
    policy = apply_output_policy(
        content,
        version.output_checks or {},
        version.transforms or {},
    )
    audit = {
        "rule_set_version_id": version.id,
        "rule_output_action": policy.action,
        "rule_output_transformed": policy.transformed,
        "rule_output_hits": list(policy.hits),
        "fallback_quality_contract": "emoji_text_safety_only",
    }
    if not policy.allowed:
        return CommentQualityDecision(
            False,
            "",
            "fallback_outbound_policy_blocked",
            policy.reason or "固定规则版本拒绝评论兜底",
            audit,
        )
    return _outbound_decision(
        session,
        action,
        payload=payload,
        content=policy.content,
        audit=audit,
    )


def _unavailable_comment_decision() -> CommentQualityDecision:
    return CommentQualityDecision(
        False,
        "",
        "comment_unavailable_message",
        "频道源消息不存在或评论区已关闭",
    )


def _lock_channel_message(session: Session, action: Action, payload: PostCommentPayload) -> bool:
    statement = select(ChannelMessage.id).where(
        ChannelMessage.id == payload.channel_message_id,
        ChannelMessage.tenant_id == action.tenant_id,
        ChannelMessage.channel_target_id == payload.channel_target_id,
        ChannelMessage.message_id == payload.message_id,
        ChannelMessage.comment_available.is_(True),
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    return session.scalar(statement) is not None


def _fixed_rule_version(
    session: Session,
    action: Action,
    payload: PostCommentPayload,
) -> tuple[RuleSetVersion | None, CommentQualityDecision | None]:
    resolved_version_id = int(payload.resolved_rule_set_version_id or 0)
    configured_version_id = int(payload.rule_set_version_id or 0)
    rule_set_id = int(payload.rule_set_id or 0)
    rule_set_version = int(payload.rule_set_version or 0)
    version_id = resolved_version_id or configured_version_id
    version = session.get(RuleSetVersion, version_id) if version_id else None
    matches = bool(
        version
        and version.tenant_id == action.tenant_id
        and version.status in FIXED_RULE_SNAPSHOT_STATUSES
        and resolved_version_id == configured_version_id == version.id
        and rule_set_id == version.rule_set_id
        and rule_set_version == version.version
    )
    if matches:
        return version, None
    return None, CommentQualityDecision(
        False,
        "",
        "rule_version_unavailable",
        "Action 固定规则快照不存在、状态非法或绑定字段不匹配",
        {"rule_set_version_id": version_id},
    )


def _same_message_comment_history(
    session: Session,
    action: Action,
    payload: PostCommentPayload,
) -> list[str]:
    managed_texts = _managed_history_texts(session, action, payload)
    remote = session.scalars(
        select(ChannelMessageComment.content_preview)
        .where(
            ChannelMessageComment.tenant_id == action.tenant_id,
            ChannelMessageComment.channel_target_id == payload.channel_target_id,
            ChannelMessageComment.channel_message_id == payload.channel_message_id,
            ChannelMessageComment.content_preview != "",
        )
        .order_by(ChannelMessageComment.created_at.desc())
    )
    return [*managed_texts, *(str(item).strip() for item in remote if str(item).strip())]


def _managed_history_texts(
    session: Session,
    action: Action,
    payload: PostCommentPayload,
) -> list[dict]:
    target_key, target_value = _payload_history_key(session, "channel_target_id", payload.channel_target_id)
    channel_key, channel_value = _payload_history_key(session, "channel_message_id", payload.channel_message_id)
    message_key, message_value = _payload_history_key(session, "message_id", payload.message_id)
    empty_id = "0" if session.get_bind().dialect.name == "postgresql" else 0
    content = Action.payload["comment_text"].as_string()
    common = (
        Action.id != action.id,
        Action.tenant_id == action.tenant_id,
        Action.action_type == "post_comment",
        Action.status.in_(OPEN_COMMENT_HISTORY_STATUSES),
        func.trim(content) != "",
        target_key == target_value,
    )
    modern = list(session.scalars(
        select(content).where(*common, channel_key == channel_value).order_by(Action.created_at.desc())
    ))
    legacy = session.scalars(select(content).where(
        *common,
        message_key == message_value,
        or_(channel_key.is_(None), channel_key == "", channel_key == empty_id),
    ).order_by(Action.created_at.desc()))
    return [str(item).strip() for item in (*modern, *legacy) if str(item).strip()]


def _payload_history_key(session: Session, key: str, value: int | None):
    expression = Action.payload[key]
    if session.get_bind().dialect.name == "postgresql":
        return expression.as_string(), str(int(value or 0))
    return expression.as_integer(), int(value or 0)


def _outbound_decision(
    session: Session,
    action: Action,
    *,
    payload: PostCommentPayload,
    content: str,
    audit: dict,
) -> CommentQualityDecision:
    channel = session.get(OperationTarget, int(payload.channel_target_id or 0))
    group = session.scalar(select(TgGroup).where(
        TgGroup.tenant_id == action.tenant_id,
        TgGroup.tg_peer_id == (channel.tg_peer_id if channel else ""),
    ))
    if not group:
        return CommentQualityDecision(False, "", "peer_invalid", "频道评论缺少可校验的讨论组", audit)
    filtered = filter_outbound_content(
        session,
        tenant_id=action.tenant_id,
        group=group,
        content=content,
    )
    if not filtered.ok:
        return CommentQualityDecision(False, "", "content_rejected", filtered.reason, audit)
    return CommentQualityDecision(True, filtered.content, audit=audit)


__all__ = [
    "CommentQualityDecision",
    "evaluate_comment_fallback_quality",
    "evaluate_comment_generation_quality",
]
