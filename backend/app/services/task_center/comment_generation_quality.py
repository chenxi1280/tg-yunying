from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Action, ChannelMessage, ChannelMessageComment, OperationTarget, RuleSetVersion, TgGroup
from app.services.content_filters import filter_outbound_content
from app.services.rule_engine import apply_output_policy

from .ai_generator import clean_channel_comment_contents
from .channel_payloads import PostCommentPayload


COMMENT_HISTORY_LIMIT = 50
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
    _lock_channel_message(session, action, payload)
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


def _lock_channel_message(session: Session, action: Action, payload: PostCommentPayload) -> None:
    statement = select(ChannelMessage.id).where(
        ChannelMessage.id == payload.channel_message_id,
        ChannelMessage.tenant_id == action.tenant_id,
        ChannelMessage.channel_target_id == payload.channel_target_id,
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    session.scalar(statement)


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
    managed = session.scalars(
        select(Action.payload)
        .where(
            Action.id != action.id,
            Action.tenant_id == action.tenant_id,
            Action.action_type == "post_comment",
            Action.status.in_(OPEN_COMMENT_HISTORY_STATUSES),
            Action.payload["channel_target_id"].as_integer() == payload.channel_target_id,
            or_(
                Action.payload["channel_message_id"].as_integer() == payload.channel_message_id,
                Action.payload["message_id"].as_integer() == payload.message_id,
            ),
        )
        .order_by(Action.created_at.desc())
        .limit(COMMENT_HISTORY_LIMIT)
    )
    managed_texts = [
        text
        for item in managed
        if isinstance(item, dict)
        if (text := str(item.get("comment_text") or "").strip())
    ]
    remaining = max(1, COMMENT_HISTORY_LIMIT - len(managed_texts))
    remote = session.scalars(
        select(ChannelMessageComment.content_preview)
        .where(
            ChannelMessageComment.tenant_id == action.tenant_id,
            ChannelMessageComment.channel_target_id == payload.channel_target_id,
            ChannelMessageComment.channel_message_id == payload.channel_message_id,
            ChannelMessageComment.content_preview != "",
        )
        .order_by(ChannelMessageComment.created_at.desc())
        .limit(remaining)
    )
    return [*managed_texts, *(str(item).strip() for item in remote if str(item).strip())]


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
