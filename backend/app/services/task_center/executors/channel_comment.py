from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Action, ChannelMessage, ChannelMessageComment, OperationTarget, RuleSet, Task, TgAccount

from app.services.rule_engine import apply_output_policy, bound_rule_version, evaluate_input_filter
from ..account_pool import daily_uncovered_account_count, select_task_accounts
from ..ai_limits import allocate_message_budget
from ..ai_generator import AiGenerationUnavailable, clean_channel_comment_contents, generate_channel_comments, generate_channel_reply_comments
from ..channel_membership import channel_member_accounts, gate_channel_membership
from ..pacing import schedule_times
from ..payloads import PostCommentPayload, create_comment_action
from app.services.target_learning_audit import audit_learning_profile_use
from app.services.tenant_target_profile import tenant_learning_profile_preview
from .common import add_tokens, adjust_for_account_hour_limit, channel_message_action_count, channel_message_payload, channel_scope, pick_channel_account, quantity_jitter_bounds, quantity_with_jitter, record_channel_capacity_warning, stats_inc

CHANNEL_COMMENT_SCENE = "channel_comment"
MAX_COMMENT_GENERATION_BATCH_PER_MESSAGE = 4
PROFILE_SYNCED_STATUS = "已同步"
COMMENT_ACCOUNT_PROFILE_ERROR = "评论账号资料未初始化，请先在账号中心批量初始化中文昵称、username 和头像"


def build_plan(session: Session, task: Task) -> int:
    config = task.type_config or {}
    rule_version = bound_rule_version(session, task)
    rule_set = session.get(RuleSet, rule_version.rule_set_id) if rule_version else None
    channel = session.get(OperationTarget, int(config.get("target_channel_id") or 0))
    if not channel or channel.tenant_id != task.tenant_id or channel.target_type != "channel":
        task.last_error = "目标频道不存在"
        return 0
    gate = gate_channel_membership(session, task, channel, require_send=True)
    if not gate.ready:
        return gate.created
    channel, messages = channel_scope(session, task, config, comment_available_only=True)
    if not channel or not messages:
        return 0
    profile_preview = tenant_learning_profile_preview(session, task.tenant_id, CHANNEL_COMMENT_SCENE)
    audit_learning_profile_use(session, task, profile_preview, "AI评论任务")
    config = _config_with_comment_profile(config, profile_preview)
    target_per_message = int(config.get("target_comments_per_message") or 1)
    _lower, max_target_per_message = quantity_jitter_bounds(target_per_message, float(config.get("comment_count_jitter") or 0))
    account_scan_limit = max(max_target_per_message, int((task.account_config or {}).get("max_concurrent") or max_target_per_message))
    ready_accounts = channel_member_accounts(
        session,
        task,
        channel,
        select_task_accounts(
            session,
            task.tenant_id,
            task.account_config or {},
            limit=account_scan_limit,
            enforce_max_concurrent=False,
            daily_coverage_task_id=task.id,
            daily_coverage_action_types=("post_comment",),
        ),
        require_send=True,
    )
    accounts = _comment_ready_accounts(task, ready_accounts)
    if not accounts:
        task.last_error = COMMENT_ACCOUNT_PROFILE_ERROR if ready_accounts else "没有可用账号，等待账号恢复后继续执行"
        return 0
    coverage_remaining = daily_uncovered_account_count(session, task.id, ("post_comment",), accounts)
    actions: list[tuple[ChannelMessage, str, dict | None]] = []
    reply_min_by_message: dict[int, int] = {}
    requested_reply_targets = [int(item) for item in config.get("reply_to_message_ids") or [] if int(item or 0) > 0]
    comment_mode = config.get("comment_mode") or "comment"
    reply_targets = _valid_reply_targets(session, task, channel.id, messages, requested_reply_targets)
    if comment_mode in {"reply", "mixed"} and requested_reply_targets and not reply_targets:
        task.last_error = "回复对象不属于当前频道消息，请先采集评论后重新选择"
        return 0
    quality_skipped = False
    for message, quantity in _message_comment_quantities(
        session,
        task,
        config,
        messages,
        daily_coverage_min_total=coverage_remaining,
    ):
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
        reply_min = min(quantity, int(config.get("reply_min_per_message") or 0))
        reply_target_pool = _message_reply_targets(session, task, channel.id, message)
        if reply_min > len(reply_target_pool):
            stats_inc(task, "reply_target_shortfall_count")
            task.last_error = "可引用评论不足，等待采集到可回复评论后继续执行"
            return 0
        reply_min_by_message[message.id] = reply_min
        minimum_targets = reply_target_pool[:reply_min]
        try:
            reply_contents, reply_tokens = _generate_minimum_reply_comments(session, task, config, minimum_targets, message, channel.title)
            normal_count = max(0, quantity - len(reply_contents))
            raw_contents, tokens = _generate_normal_channel_comments(session, task, config, normal_count, message, channel.title)
        except AiGenerationUnavailable as exc:
            task.last_error = str(exc)
            return 0
        contents = clean_channel_comment_contents(raw_contents, _recent_comment_texts(session, task, message), limit=quantity)
        if raw_contents and not contents:
            quality_skipped = True
            stats_inc(task, "skipped_count")
            continue
        add_tokens(task, tokens + reply_tokens)
        actions.extend((message, content, target) for content, target in zip(reply_contents, minimum_targets, strict=False))
        actions.extend((message, content, _reply_target_for_index(comment_mode, reply_targets, index)) for index, content in enumerate(contents))
    if not actions:
        task.last_error = "AI 评论候选语义重复或模板化，已跳过本轮" if quality_skipped else ""
        return 0
    record_channel_capacity_warning(task, "回复", target_per_message, len(accounts))
    times = schedule_times(len(actions), task.pacing_config or {})
    prepared_actions: list[tuple[int, object, PostCommentPayload]] = []
    created = 0
    for index, (message, content, reply_target) in enumerate(actions):
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
        prepared_actions.append(
            (
                account.id,
                planned_at,
                PostCommentPayload(
                    **channel_message_payload(channel, message),
                    comment_text=content,
                    comment_mode="reply" if reply_target else "comment",
                    reply_to_message_id=_reply_target_message_id(reply_target),
                    reply_target_label=_reply_target_label(reply_target),
                    reply_target_author=_reply_target_text(reply_target, "author"),
                    reply_target_preview=_reply_target_text(reply_target, "preview"),
                    reply_target_source=_reply_target_text(reply_target, "source"),
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
        )
    prepared_reply_counts: dict[int, int] = {}
    for _account_id, _planned_at, payload in prepared_actions:
        if payload.reply_to_message_id:
            prepared_reply_counts[payload.channel_message_id] = prepared_reply_counts.get(payload.channel_message_id, 0) + 1
    if any(prepared_reply_counts.get(message_id, 0) < required for message_id, required in reply_min_by_message.items()):
        stats_inc(task, "reply_candidate_shortfall_count")
        task.last_error = "AI 引用评论候选不足，已跳过本轮"
        return 0
    stats = dict(task.stats or {})
    stats["reply_planned_count"] = sum(prepared_reply_counts.values())
    task.stats = stats
    for account_id, planned_at, payload in prepared_actions:
        create_comment_action(session, task, account_id, planned_at, payload)
        created += 1
    return created


def _generate_minimum_reply_comments(
    session: Session,
    task: Task,
    config: dict,
    reply_targets: list[dict],
    message: ChannelMessage,
    target_label: str,
) -> tuple[list[str], int]:
    if not reply_targets:
        return [], 0
    contents, tokens = generate_channel_reply_comments(
        session,
        task.tenant_id,
        config,
        reply_targets=reply_targets,
        message_content=message.content_preview or message.message_url,
        target_label=target_label,
    )
    if len(contents) < len(reply_targets):
        stats_inc(task, "reply_candidate_shortfall_count")
        raise AiGenerationUnavailable("AI 引用评论候选不足，已跳过本轮")
    return contents, tokens


def _generate_normal_channel_comments(
    session: Session,
    task: Task,
    config: dict,
    count: int,
    message: ChannelMessage,
    target_label: str,
) -> tuple[list[str], int]:
    if count <= 0:
        return [], 0
    return generate_channel_comments(
        session,
        task.tenant_id,
        config,
        count=count,
        message_content=message.content_preview or message.message_url,
        target_label=target_label,
    )


def _message_comment_quantities(
    session: Session,
    task: Task,
    config: dict,
    messages: list[ChannelMessage],
    *,
    daily_coverage_min_total: int = 0,
) -> list[tuple[ChannelMessage, int]]:
    managed_usernames = _tenant_account_usernames(session, task.tenant_id)
    deficits = [_message_comment_deficit(session, task, config, message, managed_usernames) for message in messages]
    coverage_floor = min(max(0, int(daily_coverage_min_total or 0)), sum(deficits))
    deficits = _apply_daily_coverage_minimum(deficits, coverage_floor)
    budget = int((task.pacing_config or {}).get("max_actions_per_hour") or 0)
    quantities = allocate_message_budget(deficits, budget) if budget > 0 else deficits
    capped = [min(quantity, MAX_COMMENT_GENERATION_BATCH_PER_MESSAGE) for quantity in quantities]
    return list(zip(messages, capped, strict=False))


def _apply_daily_coverage_minimum(deficits: list[int], minimum: int) -> list[int]:
    adjusted = [max(0, int(deficit or 0)) for deficit in deficits]
    remaining = max(0, int(minimum or 0) - sum(adjusted))
    if not adjusted or remaining <= 0:
        return adjusted
    index = 0
    while remaining > 0:
        adjusted[index % len(adjusted)] += 1
        remaining -= 1
        index += 1
    return adjusted


def _message_comment_deficit(session: Session, task: Task, config: dict, message: ChannelMessage, managed_usernames: set[str]) -> int:
    desired = quantity_with_jitter(int(config.get("target_comments_per_message") or 1), float(config.get("comment_count_jitter") or 0))
    used_count = max(
        channel_message_action_count(session, task, "post_comment", message),
        _collected_managed_comment_count(session, task, message, managed_usernames),
    )
    return max(0, desired - used_count)


def _collected_managed_comment_count(session: Session, task: Task, message: ChannelMessage, managed_usernames: set[str]) -> int:
    if not managed_usernames:
        return 0
    return int(
        session.scalar(
            select(func.count(ChannelMessageComment.id)).where(
                ChannelMessageComment.tenant_id == task.tenant_id,
                ChannelMessageComment.channel_target_id == message.channel_target_id,
                ChannelMessageComment.channel_message_id == message.id,
                func.lower(ChannelMessageComment.author_username).in_(managed_usernames),
            )
        )
        or 0
    )


def _tenant_account_usernames(session: Session, tenant_id: int) -> set[str]:
    rows = session.scalars(
        select(TgAccount.username).where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.username.is_not(None),
        )
    )
    return {str(username or "").strip().lstrip("@").lower() for username in rows if str(username or "").strip()}


def _comment_ready_accounts(task: Task, accounts: list) -> list:
    ready = [account for account in accounts if _comment_account_profile_ready(account)]
    blocked_count = len(accounts) - len(ready)
    stats = dict(task.stats or {})
    if blocked_count:
        stats["comment_profile_blocked_account_count"] = blocked_count
        stats["comment_profile_ready_account_count"] = len(ready)
        task.last_error = COMMENT_ACCOUNT_PROFILE_ERROR
    else:
        stats.pop("comment_profile_blocked_account_count", None)
        stats.pop("comment_profile_ready_account_count", None)
        if task.last_error == COMMENT_ACCOUNT_PROFILE_ERROR:
            task.last_error = ""
    task.stats = stats
    return ready


def _comment_account_profile_ready(account) -> bool:
    return all(
        [
            _has_chinese_text(account.tg_first_name),
            bool(str(account.username or "").strip()),
            bool(str(account.avatar_object_key or "").strip()),
            str(account.profile_sync_status or "").strip() == PROFILE_SYNCED_STATUS,
        ]
    )


def _has_chinese_text(value: str | None) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in str(value or ""))


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


def _reply_target_for_index(comment_mode: str, reply_targets: list[dict], index: int) -> dict | None:
    if not reply_targets:
        return None
    if comment_mode == "reply":
        return reply_targets[index] if index < len(reply_targets) else None
    if comment_mode == "mixed" and index % 2 == 1:
        target_index = index // 2
        return reply_targets[target_index] if target_index < len(reply_targets) else None
    return None


def _valid_reply_targets(session: Session, task: Task, channel_target_id: int, messages: list[ChannelMessage], requested_ids: list[int]) -> list[dict]:
    if not requested_ids:
        return []
    channel_message_ids = [message.id for message in messages]
    if not channel_message_ids:
        return []
    comments = session.scalars(
        select(ChannelMessageComment).where(
            ChannelMessageComment.tenant_id == task.tenant_id,
            ChannelMessageComment.channel_target_id == channel_target_id,
            ChannelMessageComment.channel_message_id.in_(channel_message_ids),
            ChannelMessageComment.comment_message_id.in_(requested_ids),
        )
    )
    by_id = {int(comment.comment_message_id): _reply_target_from_comment(comment) for comment in comments}
    seen: set[int] = set()
    filtered: list[dict] = []
    for target_id in requested_ids:
        if target_id in by_id and target_id not in seen:
            filtered.append(by_id[target_id])
            seen.add(target_id)
    return filtered


def _message_reply_targets(session: Session, task: Task, channel_target_id: int, message: ChannelMessage, *, limit: int = 20) -> list[dict]:
    used_ids = _used_channel_reply_target_ids(session, task, channel_target_id, message)
    limit_value = max(1, int(limit))
    comment_query = (
        select(ChannelMessageComment)
        .where(
            ChannelMessageComment.tenant_id == task.tenant_id,
            ChannelMessageComment.channel_target_id == channel_target_id,
            ChannelMessageComment.channel_message_id == message.id,
        )
    )
    if used_ids:
        comment_query = comment_query.where(~ChannelMessageComment.comment_message_id.in_(used_ids))
    comments = session.scalars(
        comment_query.order_by(ChannelMessageComment.created_at.asc(), ChannelMessageComment.id.asc()).limit(limit_value)
    )
    targets = [_reply_target_from_comment(comment) for comment in comments]
    targets.extend(_historical_channel_reply_targets(session, task, channel_target_id, message, limit=limit_value + len(used_ids)))
    return _exclude_used_reply_targets(_dedupe_reply_targets(targets), used_ids)


def _dedupe_reply_targets(targets: list[dict]) -> list[dict]:
    seen: set[int] = set()
    deduped: list[dict] = []
    for target in targets:
        message_id = int(target.get("message_id") or 0)
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)
        deduped.append(target)
    return deduped


def _exclude_used_reply_targets(targets: list[dict], used_ids: set[int]) -> list[dict]:
    if not used_ids:
        return targets
    return [target for target in targets if int(target.get("message_id") or 0) not in used_ids]


def _used_channel_reply_target_ids(session: Session, task: Task, channel_target_id: int, message: ChannelMessage) -> set[int]:
    actions = session.scalars(
        select(Action).where(
            Action.task_id == task.id,
            Action.task_type == "channel_comment",
            Action.action_type == "post_comment",
        )
    )
    used_ids: set[int] = set()
    for action in actions:
        if _payload_int(action, "channel_target_id") != channel_target_id:
            continue
        if not _is_same_channel_message(action, message):
            continue
        reply_to_message_id = _payload_int(action, "reply_to_message_id")
        if reply_to_message_id:
            used_ids.add(reply_to_message_id)
    return used_ids


def _is_same_channel_message(action: Action, message: ChannelMessage) -> bool:
    channel_message_id = _payload_int(action, "channel_message_id")
    message_id = _payload_int(action, "message_id")
    return channel_message_id == message.id or message_id == message.message_id


def _payload_int(action: Action, key: str) -> int:
    payload = action.payload if isinstance(action.payload, dict) else {}
    raw = str(payload.get(key) or "").strip()
    return int(raw) if raw.isdigit() else 0


def _historical_channel_reply_targets(session: Session, task: Task, channel_target_id: int, message: ChannelMessage, *, limit: int = 20) -> list[dict]:
    rows = session.scalars(
        select(Action)
        .where(
            Action.task_id == task.id,
            Action.task_type == "channel_comment",
            Action.action_type == "post_comment",
            Action.status == "success",
            Action.payload["channel_target_id"].as_integer() == channel_target_id,
            or_(
                Action.payload["channel_message_id"].as_integer() == message.id,
                Action.payload["message_id"].as_integer() == message.message_id,
            ),
        )
        .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
        .limit(max(1, int(limit)))
    )
    return [target for action in rows if (target := _reply_target_from_comment_action(action))]


def _reply_target_from_comment_action(action: Action) -> dict | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    result = action.result if isinstance(action.result, dict) else {}
    raw_id = str(result.get("telegram_msg_id") or result.get("remote_message_id") or "").strip()
    content = str(payload.get("comment_text") or "").strip()
    if not raw_id.isdigit() or not content:
        return None
    return {
        "message_id": int(raw_id),
        "author": str(payload.get("account_role") or "历史评论账号").strip(),
        "preview": content[:120],
        "source": "own_history",
    }


def _reply_target_from_comment(comment: ChannelMessageComment) -> dict:
    return {
        "message_id": int(comment.comment_message_id),
        "author": str(comment.author_name or "读者").strip(),
        "preview": str(comment.content_preview or "").strip()[:120],
        "source": "channel_comment",
    }


def _reply_target_message_id(target: dict | None) -> int | None:
    return int(target.get("message_id")) if target and target.get("message_id") else None


def _reply_target_label(target: dict | None) -> str:
    message_id = _reply_target_message_id(target)
    return f"回复消息 #{message_id}" if message_id else ""


def _reply_target_text(target: dict | None, key: str) -> str:
    return str(target.get(key) or "") if target else ""


__all__ = ["build_plan"]
