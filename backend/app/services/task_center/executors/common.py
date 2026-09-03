from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelMessage,
    ListenerChannelSnapshotItem,
    OperationTarget,
    Task,
    TgAccount,
)
from app.services._common import _now
from app.services.account_capacity import account_capacity_decision
from ..channel_listener_runtime import (
    channel_snapshot_binding,
    ensure_channel_subscription,
)


def quantity_with_jitter(quantity: int, jitter_ratio: float | int = 0.15) -> int:
    lower, upper = quantity_jitter_bounds(quantity, jitter_ratio)
    if lower == upper:
        return lower
    return random.randint(lower, upper)


def quantity_jitter_bounds(quantity: int, jitter_ratio: float | int = 0.15) -> tuple[int, int]:
    base = max(0, int(quantity or 0))
    jitter = max(0.0, float(jitter_ratio or 0))
    if base <= 0 or jitter <= 0:
        return base, base
    lower = max(1, round(base * (1 - jitter)))
    upper = max(lower, round(base * (1 + jitter)))
    return lower, upper


def stats_inc(task: Task, key: str, amount: int = 1) -> None:
    stats = dict(task.stats or {})
    stats[key] = int(stats.get(key) or 0) + amount
    task.stats = stats


def add_tokens(task: Task, tokens: int) -> None:
    if not tokens:
        return
    stats = dict(task.stats or {})
    stats["used_ai_tokens"] = int(stats.get("used_ai_tokens") or 0) + int(tokens)
    task.stats = stats


def channel_scope(session: Session, task: Task, config: dict, *, comment_available_only: bool = False) -> tuple[OperationTarget | None, list[ChannelMessage]]:
    channel = session.get(OperationTarget, int(config.get("target_channel_id") or 0))
    if not channel or channel.tenant_id != task.tenant_id or channel.target_type != "channel":
        task.last_error = "目标频道不存在"
        return None, []
    if _channel_scope_name(config) != "specific":
        if config.get("listen_new_messages") is False:
            existing_messages = channel_messages(session, task.tenant_id, config, comment_available_only=comment_available_only)
            if existing_messages:
                return channel, existing_messages
        ensure_channel_subscription(session, task, channel)
        snapshot_status, next_probe_at, state_id, snapshot_revision = channel_snapshot_binding(
            session,
            task,
            channel,
        )
        if snapshot_status != "ready":
            task.last_error = f"channel_source_snapshot_{snapshot_status}"
            if next_probe_at is not None:
                task.next_run_at = normalize_datetime(next_probe_at)
            if task.type == "channel_view" and state_id is not None:
                existing_messages = _stale_channel_view_messages(
                    session, task, config, state_id=state_id,
                    snapshot_revision=snapshot_revision,
                    comment_available_only=comment_available_only,
                )
                if existing_messages:
                    return channel, existing_messages
            return None, []
        messages = channel_messages(
            session,
            task.tenant_id,
            config,
            comment_available_only=comment_available_only,
            listener_state_id=state_id,
            snapshot_revision=snapshot_revision,
        )
    else:
        messages = channel_messages(
            session,
            task.tenant_id,
            config,
            comment_available_only=comment_available_only,
        )
    if not messages:
        task.last_error = task.last_error or "未找到频道消息，等待下一轮采集"
        return None, []
    return channel, messages


def _stale_channel_view_messages(
    session: Session,
    task: Task,
    config: dict,
    *,
    state_id: str,
    snapshot_revision: int,
    comment_available_only: bool,
) -> list[ChannelMessage]:
    messages = channel_messages(
        session, task.tenant_id, config,
        comment_available_only=comment_available_only,
        listener_state_id=state_id,
        snapshot_revision=snapshot_revision,
    )
    return messages or _active_unbound_channel_messages(
        session, task, config,
        comment_available_only=comment_available_only,
    )


def _active_unbound_channel_messages(
    session: Session,
    task: Task,
    config: dict,
    *,
    comment_available_only: bool,
) -> list[ChannelMessage]:
    messages = channel_messages(
        session,
        task.tenant_id,
        config,
        comment_available_only=comment_available_only,
    )
    active_days = int(config.get("message_active_days") or 7)
    if active_days <= 0:
        active_days = 7
    cutoff = _now().replace(tzinfo=None) - timedelta(days=active_days)
    return [
        message for message in messages
        if message.published_at
        and message.published_at.replace(tzinfo=None) >= cutoff
    ]


def collect_channel_messages(session: Session, task: Task, channel: OperationTarget, config: dict) -> int:
    del config
    ensure_channel_subscription(session, task, channel)
    return 0


def channel_fetch_limit(config: dict) -> int:
    scope = _channel_scope_name(config)
    if scope in {"latest_n", "dynamic_new"}:
        return max(1, min(100, int(config.get("latest_message_count") or config.get("message_count") or 10)))
    return 50


def channel_message_url(channel: OperationTarget, message_id: int) -> str:
    if channel.username:
        return f"https://t.me/{channel.username}/{message_id}"
    if channel.tg_peer_id.startswith("-100") and channel.tg_peer_id[4:].isdigit():
        return f"https://t.me/c/{channel.tg_peer_id[4:]}/{message_id}"
    return ""


def normalize_datetime(value) -> datetime | None:
    if not value:
        return None
    parsed = parse_datetime(value)
    return parsed.replace(tzinfo=None) if parsed and parsed.tzinfo else parsed


def channel_message_payload(channel: OperationTarget, message: ChannelMessage) -> dict:
    return {
        "channel_id": channel.tg_peer_id,
        "channel_target_id": channel.id,
        "target_reference_revision": int(channel.reference_revision or 1),
        "target_reference_snapshot": {
            "tg_peer_id": str(channel.tg_peer_id),
            "username": str(channel.username or ""),
            "title": str(channel.title),
        },
        "channel_message_id": message.id,
        "message_id": message.message_id,
        "target_display": channel.title,
        "message_content": message.content_preview,
    }


def planned_channel_message_ids(session: Session, task: Task, action_type: str) -> set[int]:
    planned: set[int] = set()
    for payload in session.scalars(select(Action.payload).where(Action.task_id == task.id, Action.action_type == action_type)):
        if not isinstance(payload, dict):
            continue
        channel_message_id = payload.get("channel_message_id")
        if isinstance(channel_message_id, int):
            planned.add(channel_message_id)
    return planned


def unplanned_channel_messages(session: Session, task: Task, action_type: str, messages: list[ChannelMessage]) -> list[ChannelMessage]:
    planned = planned_channel_message_ids(session, task, action_type)
    return [message for message in messages if message.id not in planned]


def channel_message_account_ids(
    session: Session,
    task: Task,
    action_type: str,
    message: ChannelMessage,
    *,
    execution_date: str | None = None,
    include_skipped_codes: set[str] | None = None,
) -> set[int]:
    account_ids_by_message = channel_message_account_ids_for_messages(
        session,
        task,
        action_type,
        [message],
        execution_date=execution_date,
        include_skipped_codes=include_skipped_codes,
    )
    return account_ids_by_message[message.id]


def channel_message_account_ids_for_messages(
    session: Session,
    task: Task,
    action_type: str,
    messages: list[ChannelMessage],
    *,
    execution_date: str | None = None,
    include_skipped_codes: set[str] | None = None,
) -> dict[int, set[int]]:
    from .channel_action_history import channel_message_account_ids_for_messages as load_account_ids

    return load_account_ids(
        session,
        task,
        action_type,
        messages,
        execution_date=execution_date,
        include_skipped_codes=include_skipped_codes,
    )


def channel_message_action_count(session: Session, task: Task, action_type: str, message: ChannelMessage) -> int:
    count = 0
    for payload in session.scalars(
        select(Action.payload).where(
            Action.task_id == task.id,
            Action.action_type == action_type,
            Action.status.in_(["pending", "executing", "success", "failed"]),
        )
    ):
        if not isinstance(payload, dict):
            continue
        if payload.get("channel_message_id") == message.id or payload.get("message_id") == message.message_id:
            count += 1
    return count


def available_channel_accounts_for_message(session: Session, task: Task, action_type: str, message: ChannelMessage, accounts: list[TgAccount]) -> list[TgAccount]:
    used = channel_message_account_ids(session, task, action_type, message)
    return [account for account in accounts if account.id not in used]


def available_channel_accounts_for_message_date(session: Session, task: Task, action_type: str, message: ChannelMessage, accounts: list[TgAccount], execution_date: str) -> list[TgAccount]:
    used = channel_message_account_ids(session, task, action_type, message, execution_date=execution_date)
    return [account for account in accounts if account.id not in used]


def record_channel_capacity_warning(task: Task, action_label: str, target_per_message: int, max_effective_per_message: int) -> None:
    stats = dict(task.stats or {})
    previous_warning = str(stats.get("capacity_warning") or "")
    if target_per_message <= max_effective_per_message:
        stats.pop("capacity_warning", None)
        if previous_warning and task.last_error == previous_warning:
            task.last_error = ""
        task.stats = stats
        return
    warning = f"每条消息目标{action_label} {target_per_message}，当前参与账号 {max_effective_per_message} 个；任务会继续运行，账号恢复或增加后继续补计划。"
    stats["capacity_warning"] = warning
    stats["target_per_message"] = target_per_message
    stats["max_effective_per_message"] = max_effective_per_message
    task.stats = stats
    if previous_warning and task.last_error == previous_warning:
        task.last_error = ""


def channel_messages(
    session: Session,
    tenant_id: int,
    config: dict,
    *,
    comment_available_only: bool = False,
    listener_state_id: str | None = None,
    snapshot_revision: int = 0,
) -> list[ChannelMessage]:
    stmt = select(ChannelMessage).where(ChannelMessage.tenant_id == tenant_id, ChannelMessage.channel_target_id == int(config.get("target_channel_id") or 0))
    if listener_state_id is not None:
        stmt = stmt.join(
            ListenerChannelSnapshotItem,
            ListenerChannelSnapshotItem.channel_message_id == ChannelMessage.id,
        ).where(
            ListenerChannelSnapshotItem.listener_source_state_id == listener_state_id,
            ListenerChannelSnapshotItem.snapshot_revision == snapshot_revision,
        )
    if comment_available_only:
        stmt = stmt.where(ChannelMessage.comment_available.is_(True))
    scope = _channel_scope_name(config)
    ids = [int(item) for item in config.get("message_ids") or []]
    if scope == "specific" and ids:
        stmt = stmt.where(or_(ChannelMessage.id.in_(ids), ChannelMessage.message_id.in_(ids)))
    elif scope == "today_new":
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        stmt = stmt.where(ChannelMessage.published_at >= today_start)
    elif scope == "date_range":
        date_from = parse_datetime(config.get("date_from"))
        date_to = parse_datetime(config.get("date_to"))
        if date_from:
            stmt = stmt.where(ChannelMessage.published_at >= date_from)
        if date_to:
            stmt = stmt.where(ChannelMessage.published_at <= date_to)
    stmt = stmt.order_by(ChannelMessage.published_at.desc().nullslast(), ChannelMessage.id.desc())
    if scope in {"latest_n", "dynamic_new"}:
        stmt = stmt.limit(int(config.get("latest_message_count") or config.get("message_count") or 10))
    return list(session.scalars(stmt))


def _channel_scope_name(config: dict) -> str:
    initial_scope = config.get("initial_message_scope")
    if initial_scope == "new_only":
        return "dynamic_new"
    if initial_scope:
        return str(initial_scope)
    return str(config.get("message_scope") or "latest_n")


def parse_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def pick_channel_account(session: Session, task: Task, accounts, action_type: str, scheduled_at: datetime, config: dict, offset: int):
    for index in range(len(accounts)):
        account = accounts[(offset + index) % len(accounts)]
        if account_has_hour_capacity(session, task, account.id, action_type, scheduled_at, config) and account_capacity_decision(
            session,
            tenant_id=task.tenant_id,
            account_id=account.id,
            scheduled_at=scheduled_at,
        ).available:
            return account
    return accounts[offset % len(accounts)] if accounts else None


def adjust_for_account_hour_limit(session: Session, task: Task, account_id: int, action_type: str, scheduled_at: datetime, config: dict) -> datetime:
    cursor = scheduled_at
    for _ in range(24 * 7):
        decision = account_capacity_decision(session, tenant_id=task.tenant_id, account_id=account_id, scheduled_at=cursor)
        if account_has_hour_capacity(session, task, account_id, action_type, cursor, config) and decision.available:
            return cursor
        if decision.defer_until and decision.defer_until > cursor:
            cursor = decision.defer_until
            continue
        cursor += timedelta(hours=1)
    return cursor


def account_has_hour_capacity(session: Session, task: Task, account_id: int, action_type: str, scheduled_at: datetime, config: dict) -> bool:
    limit_key = {
        "like_message": "max_likes_per_account_per_hour",
        "post_comment": "max_comments_per_account_per_hour",
    }.get(action_type)
    if not limit_key:
        return True
    limit = int(config.get(limit_key) or 0)
    if limit <= 0:
        return True
    hour_start = scheduled_at.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + timedelta(hours=1)
    count = session.scalar(
        select(func.count(Action.id)).where(
            Action.tenant_id == task.tenant_id,
            Action.account_id == account_id,
            Action.action_type == action_type,
            Action.status.in_(["pending", "executing", "success"]),
            Action.scheduled_at >= hour_start,
            Action.scheduled_at < hour_end,
        )
    ) or 0
    return int(count) < limit


__all__ = [
    "add_tokens",
    "adjust_for_account_hour_limit",
    "channel_message_payload",
    "channel_message_account_ids_for_messages",
    "channel_scope",
    "collect_channel_messages",
    "available_channel_accounts_for_message",
    "available_channel_accounts_for_message_date",
    "channel_message_action_count",
    "pick_channel_account",
    "planned_channel_message_ids",
    "quantity_jitter_bounds",
    "quantity_with_jitter",
    "record_channel_capacity_warning",
    "stats_inc",
    "unplanned_channel_messages",
]
