from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Action, ChannelMessage, ChannelMessageComment, OperationTarget, RuleSet, Task

from app.services.rule_engine import apply_output_policy, bound_rule_version, evaluate_input_filter
from ..account_pool import select_task_accounts
from ..ai_limits import allocate_message_budget
from ..ai_generator import AiGenerationUnavailable, clean_channel_comment_contents, generate_channel_comments
from ..channel_membership import channel_member_accounts, gate_channel_membership
from ..pacing import schedule_times
from ..payloads import PostCommentPayload, create_comment_action
from app.services.target_learning_audit import audit_learning_profile_use
from app.services.tenant_target_profile import tenant_learning_profile_preview
from .common import add_tokens, adjust_for_account_hour_limit, channel_message_action_count, channel_message_payload, channel_scope, pick_channel_account, quantity_jitter_bounds, quantity_with_jitter, record_channel_capacity_warning, stats_inc

CHANNEL_COMMENT_SCENE = "channel_comment"
MAX_COMMENT_GENERATION_BATCH_PER_MESSAGE = 4


def build_plan(session: Session, task: Task) -> int:
    config = task.type_config or {}
    rule_version = bound_rule_version(session, task)
    rule_set = session.get(RuleSet, rule_version.rule_set_id) if rule_version else None
    channel = session.get(OperationTarget, int(config.get("target_channel_id") or 0))
    if not channel or channel.tenant_id != task.tenant_id or channel.target_type != "channel":
        task.last_error = "目标频道不存在"
        return 0
    gate = gate_channel_membership(session, task, channel)
    if not gate.ready:
        return gate.created
    channel, messages = channel_scope(session, task, config, comment_available_only=True)
    if not channel or not messages:
        return 0
    profile_preview = tenant_learning_profile_preview(session, task.tenant_id, CHANNEL_COMMENT_SCENE)
    audit_learning_profile_use(session, task, profile_preview, "AI评论任务")
    config = _config_with_comment_profile(config, profile_preview)
    actions: list[tuple[ChannelMessage, str, int | None]] = []
    requested_reply_targets = [int(item) for item in config.get("reply_to_message_ids") or [] if int(item or 0) > 0]
    comment_mode = config.get("comment_mode") or "comment"
    reply_targets = _valid_reply_targets(session, task, channel.id, messages, requested_reply_targets)
    if comment_mode in {"reply", "mixed"} and requested_reply_targets and not reply_targets:
        task.last_error = "回复对象不属于当前频道消息，请先采集评论后重新选择"
        return 0
    quality_skipped = False
    for message, quantity in _message_comment_quantities(session, task, config, messages):
        if rule_version:
            context_text = "\n".join(
                item
                for item in [
                    message.content_preview or message.message_url,
                    config.get("topic_hint") or "",
                    config.get("comment_style") or "",
                ]
                if item
            )
            input_result = evaluate_input_filter(context_text, message_type="channel_comment", filters=rule_version.filters or {})
            if not input_result.passed:
                stats_inc(task, "skipped_count")
                continue
        if not quantity:
            continue
        try:
            raw_contents, tokens = generate_channel_comments(
                session,
                task.tenant_id,
                config,
                count=quantity,
                message_content=message.content_preview or message.message_url,
                target_label=channel.title,
            )
        except AiGenerationUnavailable as exc:
            task.last_error = str(exc)
            return 0
        contents = clean_channel_comment_contents(raw_contents, _recent_comment_texts(session, task, message), limit=quantity)
        if raw_contents and not contents:
            quality_skipped = True
            stats_inc(task, "skipped_count")
            continue
        add_tokens(task, tokens)
        actions.extend((message, content, _reply_target_for_index(comment_mode, reply_targets, index)) for index, content in enumerate(contents))
    if not actions:
        task.last_error = "AI 评论候选语义重复或模板化，已跳过本轮" if quality_skipped else ""
        return 0
    target_per_message = int(config.get("target_comments_per_message") or 1)
    _lower, max_target_per_message = quantity_jitter_bounds(target_per_message, float(config.get("comment_count_jitter") or 0))
    account_scan_limit = max(len(actions), max_target_per_message, int((task.account_config or {}).get("max_concurrent") or max_target_per_message))
    accounts = channel_member_accounts(
        session,
        task,
        channel,
        select_task_accounts(
            session,
            task.tenant_id,
            task.account_config or {},
            limit=account_scan_limit,
            enforce_max_concurrent=False,
        ),
    )
    if not accounts:
        task.last_error = "没有可用账号，等待账号恢复后继续执行"
        return 0
    record_channel_capacity_warning(task, "回复", target_per_message, len(accounts))
    times = schedule_times(len(actions), task.pacing_config or {})
    created = 0
    for index, (message, content, reply_to_message_id) in enumerate(actions):
        if rule_version:
            policy_result = apply_output_policy(content, rule_version.output_checks or {}, rule_version.transforms or {})
            if not policy_result.allowed:
                stats_inc(task, "failure_count")
                continue
            content = policy_result.content
        planned_at = times[index]
        account = pick_channel_account(session, task, accounts, "post_comment", planned_at, config, index)
        if not account:
            stats_inc(task, "failure_count")
            continue
        planned_at = adjust_for_account_hour_limit(session, task, account.id, "post_comment", planned_at, config)
        create_comment_action(
            session,
            task,
            account.id,
            planned_at,
            PostCommentPayload(
                **channel_message_payload(channel, message),
                comment_text=content,
                comment_mode="reply" if reply_to_message_id else "comment",
                reply_to_message_id=reply_to_message_id,
                reply_target_label=f"回复消息 #{reply_to_message_id}" if reply_to_message_id else "",
                review_approved=True,
                rule_set_id=rule_version.rule_set_id if rule_version else None,
                rule_set_name=rule_set.name if rule_set else "",
                rule_set_version_id=rule_version.id if rule_version else None,
                resolved_rule_set_version_id=rule_version.id if rule_version else None,
                rule_set_version=rule_version.version if rule_version else None,
                rule_binding_mode="fixed_version" if rule_version and config.get("rule_set_version_id") else "follow_current" if rule_version else "",
                profile_scene=str(profile_preview.get("profile_scene") or CHANNEL_COMMENT_SCENE),
                profile_version=int(profile_preview.get("profile_version") or 0),
                profile_hit_summary=str(profile_preview.get("profile_hit_summary") or ""),
                profile_unavailable_reason=str(profile_preview.get("profile_unavailable_reason") or ""),
            ),
        )
        created += 1
    return created


def _message_comment_quantities(session: Session, task: Task, config: dict, messages: list[ChannelMessage]) -> list[tuple[ChannelMessage, int]]:
    deficits = [_message_comment_deficit(session, task, config, message) for message in messages]
    budget = int((task.pacing_config or {}).get("max_actions_per_hour") or 0)
    quantities = allocate_message_budget(deficits, budget) if budget > 0 else deficits
    capped = [min(quantity, MAX_COMMENT_GENERATION_BATCH_PER_MESSAGE) for quantity in quantities]
    return list(zip(messages, capped, strict=False))


def _message_comment_deficit(session: Session, task: Task, config: dict, message: ChannelMessage) -> int:
    desired = quantity_with_jitter(int(config.get("target_comments_per_message") or 1), float(config.get("comment_count_jitter") or 0))
    used_count = channel_message_action_count(session, task, "post_comment", message)
    return max(0, desired - used_count)


def _config_with_comment_profile(config: dict, profile_preview: dict) -> dict:
    summary = str(profile_preview.get("profile_hit_summary") or "").strip()
    if not summary:
        return dict(config)
    return {**config, "target_comment_profile": summary, "comment_style": "；".join(part for part in (str(config.get("comment_style") or ""), f"目标评论画像：{summary}") if part)}


def _recent_comment_texts(session: Session, task: Task, message: ChannelMessage, *, limit: int = 20) -> list[str]:
    rows = session.scalars(
        select(Action)
        .where(
            Action.task_id == task.id,
            Action.task_type == "channel_comment",
            Action.action_type == "post_comment",
            Action.status.in_(["pending", "executing", "success"]),
            or_(
                Action.payload["channel_message_id"].as_integer() == message.id,
                Action.payload["message_id"].as_integer() == message.message_id,
            ),
        )
        .order_by(Action.created_at.desc())
        .limit(max(1, int(limit)))
    )
    comments: list[str] = []
    for action in rows:
        payload = action.payload if isinstance(action.payload, dict) else {}
        if int(payload.get("channel_message_id") or 0) not in {0, message.id}:
            continue
        text = str(payload.get("comment_text") or "").strip()
        if text:
            comments.append(text)
    return comments


def _reply_target_for_index(comment_mode: str, reply_targets: list[int], index: int) -> int | None:
    if not reply_targets:
        return None
    if comment_mode == "reply":
        return reply_targets[index % len(reply_targets)]
    if comment_mode == "mixed" and index % 2 == 1:
        return reply_targets[(index // 2) % len(reply_targets)]
    return None


def _valid_reply_targets(session: Session, task: Task, channel_target_id: int, messages: list[ChannelMessage], requested_ids: list[int]) -> list[int]:
    if not requested_ids:
        return []
    channel_message_ids = [message.id for message in messages]
    if not channel_message_ids:
        return []
    valid_ids = set(
        session.scalars(
            select(ChannelMessageComment.comment_message_id).where(
                ChannelMessageComment.tenant_id == task.tenant_id,
                ChannelMessageComment.channel_target_id == channel_target_id,
                ChannelMessageComment.channel_message_id.in_(channel_message_ids),
                ChannelMessageComment.comment_message_id.in_(requested_ids),
            )
        )
    )
    seen: set[int] = set()
    filtered: list[int] = []
    for target_id in requested_ids:
        if target_id in valid_ids and target_id not in seen:
            filtered.append(target_id)
            seen.add(target_id)
    return filtered


__all__ = ["build_plan"]
