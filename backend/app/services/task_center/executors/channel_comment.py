from __future__ import annotations

from dataclasses import dataclass
import hashlib

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ChannelMessage, OperationTarget, RuleSet, Task

from app.services.rule_engine import bound_rule_version, evaluate_input_filter
from ..account_pool import daily_uncovered_account_count, select_task_accounts
from ..channel_membership import channel_member_accounts, gate_channel_membership
from ..pacing import schedule_times
from ..payloads import PostCommentPayload, create_comment_action
from app.services.target_learning_audit import audit_learning_profile_use
from app.services.tenant_target_profile import tenant_learning_profile_preview
from .channel_comment_budget import (
    load_message_comment_plan_states as _load_message_comment_plan_states,
    message_comment_quantities as _message_comment_quantities,
    reconcile_lifetime_cap,
    resolved_total_comment_limit as _resolved_total_comment_limit,
    total_comment_action_count as _total_comment_action_count,
)
from .channel_comment_targets import (
    message_reply_targets as _message_reply_targets,
    reply_target_label as _reply_target_label,
    reply_target_message_id as _reply_target_message_id,
    reply_target_text as _reply_target_text,
    valid_reply_targets as _valid_reply_targets,
)
from .common import (
    adjust_for_account_hour_limit,
    channel_messages,
    channel_message_payload,
    pick_channel_account,
    quantity_jitter_bounds,
    record_channel_capacity_warning,
    stats_inc,
)

CHANNEL_COMMENT_SCENE = "channel_comment"
PROFILE_SYNCED_STATUS = "已同步"
COMMENT_ACCOUNT_PROFILE_ERROR = "评论账号资料未初始化，请先在账号中心批量初始化中文昵称、username 和头像"
POSTGRES_ADVISORY_LOCK_MASK = (1 << 63) - 1


@dataclass(frozen=True)
class CommentPlanContext:
    config: dict
    total_remaining: int
    rule_version: object
    rule_set: RuleSet | None
    channel: OperationTarget
    messages: list[ChannelMessage]
    profile_preview: dict
    accounts: list


@dataclass(frozen=True)
class CommentPlanSetup:
    context: CommentPlanContext | None
    created: int = 0


@dataclass(frozen=True)
class CommentPlanSlot:
    message: ChannelMessage
    reply_target: dict | None
    slot_index: int


def build_plan(session: Session, task: Task) -> int:
    if not _lock_comment_task(session, task):
        return 0
    setup = _comment_plan_setup(session, task)
    if not setup.context:
        return setup.created
    if not _lock_comment_task(session, task):
        return 0
    context = setup.context
    slots = _comment_plan_slots(session, task, context)
    if not slots:
        return 0
    target_per_message = int(context.config.get("target_comments_per_message") or 1)
    record_channel_capacity_warning(task, "回复", target_per_message, len(context.accounts))
    prepared = _prepare_comment_actions(session, task, context, slots)
    reply_count = sum(1 for _account_id, _planned_at, payload in prepared if payload.reply_to_message_id)
    stats = dict(task.stats or {})
    stats["reply_planned_count"] = reply_count
    task.stats = stats
    return _create_prepared_actions(session, task, prepared)


def _create_prepared_actions(
    session: Session,
    task: Task,
    prepared: list[tuple[int, object, PostCommentPayload]],
) -> int:
    count_before = _total_comment_action_count(session, task)
    for account_id, planned_at, payload in prepared:
        create_comment_action(session, task, account_id, planned_at, payload)
    count_after = _total_comment_action_count(session, task)
    return max(0, count_after - count_before)


def _lock_comment_task(session: Session, task: Task) -> bool:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(select(func.pg_advisory_xact_lock(_comment_task_lock_key(task))))
    task_id = session.scalar(
        select(Task.id)
        .where(
            Task.id == task.id,
            Task.tenant_id == task.tenant_id,
            Task.deleted_at.is_(None),
        )
    )
    return task_id is not None


def _comment_task_lock_key(task: Task) -> int:
    digest = hashlib.sha256(f"{task.tenant_id}:{task.id}:channel-comment-plan".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big") & POSTGRES_ADVISORY_LOCK_MASK


def _comment_plan_setup(session: Session, task: Task) -> CommentPlanSetup:
    config = task.type_config or {}
    total_remaining = reconcile_lifetime_cap(session, task, config)
    if total_remaining <= 0:
        return CommentPlanSetup(None)
    rule_version = bound_rule_version(session, task)
    if not rule_version:
        return CommentPlanSetup(None)
    rule_set = session.get(RuleSet, rule_version.rule_set_id) if rule_version else None
    channel = session.get(OperationTarget, int(config.get("target_channel_id") or 0))
    if not channel or channel.tenant_id != task.tenant_id or channel.target_type != "channel":
        task.last_error = "目标频道不存在"
        return CommentPlanSetup(None)
    gate = gate_channel_membership(session, task, channel, require_send=True)
    if not gate.ready:
        return CommentPlanSetup(None, gate.created)
    channel, messages = _persisted_channel_scope(session, task, config)
    if not channel or not messages:
        return CommentPlanSetup(None)
    profile_preview = tenant_learning_profile_preview(session, task.tenant_id, CHANNEL_COMMENT_SCENE)
    audit_learning_profile_use(session, task, profile_preview, "AI评论任务")
    config = _config_with_comment_profile(config, profile_preview)
    accounts = _planning_accounts(session, task, channel, config)
    if not accounts:
        return CommentPlanSetup(None)
    return CommentPlanSetup(
        CommentPlanContext(
            config=config,
            total_remaining=total_remaining,
            rule_version=rule_version,
            rule_set=rule_set,
            channel=channel,
            messages=messages,
            profile_preview=profile_preview,
            accounts=accounts,
        )
    )


def _persisted_channel_scope(
    session: Session,
    task: Task,
    config: dict,
) -> tuple[OperationTarget | None, list[ChannelMessage]]:
    channel = session.get(OperationTarget, int(config.get("target_channel_id") or 0))
    if not channel or channel.tenant_id != task.tenant_id or channel.target_type != "channel":
        task.last_error = "目标频道不存在"
        return None, []
    messages = channel_messages(
        session,
        task.tenant_id,
        config,
        comment_available_only=True,
    )
    if not messages:
        task.last_error = "未找到已采集频道消息，等待监听采集"
        return None, []
    return channel, messages


def _planning_accounts(session: Session, task: Task, channel: OperationTarget, config: dict) -> list:
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
    return accounts


def _comment_plan_slots(
    session: Session,
    task: Task,
    context: CommentPlanContext,
) -> list[CommentPlanSlot] | None:
    config = context.config
    coverage_remaining = daily_uncovered_account_count(
        session,
        task.id,
        ("post_comment",),
        context.accounts,
    )
    requested_reply_targets = [int(item) for item in config.get("reply_to_message_ids") or [] if int(item or 0) > 0]
    comment_mode = config.get("comment_mode") or "comment"
    reply_targets = _valid_reply_targets(
        session,
        task,
        context.channel.id,
        context.messages,
        requested_reply_targets,
    )
    if comment_mode in {"reply", "mixed"} and requested_reply_targets and not reply_targets:
        task.last_error = "回复对象不属于当前频道消息，请先采集评论后重新选择"
        return None
    message_states = _load_message_comment_plan_states(session, task, context.messages)
    slots: list[CommentPlanSlot] = []
    for message, quantity in _message_comment_quantities(
        session,
        task,
        config,
        context.messages,
        daily_coverage_min_total=coverage_remaining,
        total_remaining=context.total_remaining,
        message_states=message_states,
    ):
        if not quantity or not _comment_input_allowed(task, context, message):
            continue
        targets = _comment_slot_targets(session, task, context, message, quantity, reply_targets)
        if targets is None:
            return None
        offset = message_states[message.id].next_slot_index
        slots.extend(CommentPlanSlot(message, target, offset + index) for index, target in enumerate(targets))
    return slots


def _comment_input_allowed(task: Task, context: CommentPlanContext, message: ChannelMessage) -> bool:
    context_text = "\n".join(
        item
        for item in [
            message.content_preview or message.message_url,
            context.config.get("topic_hint") or "",
            context.config.get("comment_style") or "",
        ]
        if item
    )
    result = evaluate_input_filter(
        context_text,
        message_type="channel_comment",
        filters=context.rule_version.filters or {},
    )
    if result.passed:
        return True
    stats_inc(task, "skipped_count")
    return False


def _comment_slot_targets(
    session: Session,
    task: Task,
    context: CommentPlanContext,
    message: ChannelMessage,
    quantity: int,
    requested_targets: list[dict],
) -> list[dict | None] | None:
    mode = str(context.config.get("comment_mode") or "comment")
    if mode == "comment":
        return [None] * quantity
    pool = (
        [target for target in requested_targets if int(target.get("channel_message_id") or 0) == message.id]
        if mode == "reply"
        else _message_reply_targets(session, task, context.channel.id, message)
    )
    required = quantity if mode == "reply" else _reply_minimum_for_mode(mode, quantity, context.config)
    if required > len(pool):
        stats_inc(task, "reply_target_shortfall_count")
        task.last_error = "可引用评论不足，等待采集到可回复评论后继续执行"
        return None
    return [*pool[:required], *([None] * (quantity - required))]


def _prepare_comment_actions(
    session: Session,
    task: Task,
    context: CommentPlanContext,
    slots: list[CommentPlanSlot],
) -> list[tuple[int, object, PostCommentPayload]]:
    times = schedule_times(len(slots), task.pacing_config or {})
    prepared: list[tuple[int, object, PostCommentPayload]] = []
    for index, slot in enumerate(slots):
        planned_at = times[index]
        account = pick_channel_account(
            session,
            task,
            context.accounts,
            "post_comment",
            planned_at,
            context.config,
            index,
        )
        if not account:
            stats_inc(task, "failure_count")
            continue
        planned_at = adjust_for_account_hour_limit(
            session,
            task,
            account.id,
            "post_comment",
            planned_at,
            context.config,
        )
        prepared.append((account.id, planned_at, _comment_payload(task, context, slot)))
    return prepared


def _reply_minimum_for_mode(comment_mode: str, quantity: int, config: dict) -> int:
    if comment_mode not in {"reply", "mixed"}:
        return 0
    return min(quantity, int(config.get("reply_min_per_message") or 0))


def _comment_payload(task: Task, context: CommentPlanContext, slot: CommentPlanSlot) -> PostCommentPayload:
    reply_target = slot.reply_target
    slot_id = f"channel-comment:{slot.message.id}:{slot.slot_index}"
    rule_version = context.rule_version
    profile = context.profile_preview
    return PostCommentPayload(
        **channel_message_payload(context.channel, slot.message),
        comment_text="",
        comment_mode="reply" if reply_target else "comment",
        reply_to_message_id=_reply_target_message_id(reply_target),
        reply_target_label=_reply_target_label(reply_target),
        reply_target_author=_reply_target_text(reply_target, "author"),
        reply_target_preview=_reply_target_text(reply_target, "preview"),
        reply_target_source=_reply_target_text(reply_target, "source"),
        review_approved=False,
        slot_id=slot_id,
        ai_generation_id=f"{task.id}:{slot_id}",
        ai_generation_status="pending",
        rule_set_id=rule_version.rule_set_id,
        rule_set_name=context.rule_set.name if context.rule_set else "",
        rule_set_version_id=rule_version.id,
        resolved_rule_set_version_id=rule_version.id,
        rule_set_version=rule_version.version,
        rule_binding_mode="fixed_version" if context.config.get("rule_set_version_id") else "follow_current",
        profile_scene=str(profile.get("profile_scene") or CHANNEL_COMMENT_SCENE),
        profile_version=int(profile.get("profile_version") or 0),
        profile_hit_summary=str(profile.get("profile_hit_summary") or ""),
        profile_unavailable_reason=str(profile.get("profile_unavailable_reason") or ""),
    )


def _comment_ready_accounts(task: Task, accounts: list) -> list:
    ready = [account for account in accounts if _comment_account_profile_ready(account)]
    blocked_count = len(accounts) - len(ready)
    stats = dict(task.stats or {})
    if blocked_count:
        stats["comment_profile_blocked_account_count"] = blocked_count
        stats["comment_profile_ready_account_count"] = len(ready)
    else:
        stats.pop("comment_profile_blocked_account_count", None)
        stats.pop("comment_profile_ready_account_count", None)
    if ready and task.last_error == COMMENT_ACCOUNT_PROFILE_ERROR:
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
    return {**config, "target_comment_profile": summary}


__all__ = ["_resolved_total_comment_limit", "_total_comment_action_count", "build_plan"]
