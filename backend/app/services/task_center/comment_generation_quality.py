from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Action, ChannelMessage, ChannelMessageComment, OperationTarget, RuleSetVersion, TgGroup
from app.services.content_filters import filter_outbound_content
from app.services.rule_engine import apply_output_policy

from .ai_generator import clean_channel_comment_contents
from .channel_payloads import PostCommentPayload


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
    }
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
    return _outbound_decision(
        session,
        action,
        payload=payload,
        content=str(cleaned[0]),
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


__all__ = ["CommentQualityDecision", "evaluate_comment_generation_quality"]
