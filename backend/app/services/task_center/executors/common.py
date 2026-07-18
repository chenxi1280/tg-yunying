from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Action, ChannelMessage, OperationTarget, Task, TgAccount
from app.services._common import _now, gateway
from app.services.account_capacity import account_capacity_decision
from app.services.developer_apps import credentials_for_task_account

from ..account_pool import select_task_accounts
from ..listener_runtime import should_collect_listener


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
        collect_channel_messages(session, task, channel, config)
    messages = channel_messages(session, task.tenant_id, config, comment_available_only=comment_available_only)
    if not messages:
        task.last_error = task.last_error or "未找到频道消息，等待下一轮采集"
        return None, []
    return channel, messages


def collect_channel_messages(session: Session, task: Task, channel: OperationTarget, config: dict) -> int:
    if not should_collect_listener("channel", channel.id, window_seconds=int(config.get("listener_interval_seconds") or 30)):
        return 0
    limit = channel_fetch_limit(config)
    accounts = select_task_accounts(session, task.tenant_id, task.account_config or {}, limit=1)
    if not accounts:
        task.last_error = "没有可用于采集频道消息的账号"
        return 0
    account = accounts[0]
    try:
        snapshots = gateway.fetch_channel_messages(
            account.id,
            channel.tg_peer_id,
            account.session_ciphertext,
            credentials_for_task_account(session, account, task.type),
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 - keep task observable and let existing rows still run.
        task.last_error = f"采集频道消息失败: {exc}"
        return 0
    created = 0
    for snapshot in snapshots:
        if snapshot.message_id <= 0:
            continue
        existing = session.scalar(
            select(ChannelMessage).where(
                ChannelMessage.tenant_id == task.tenant_id,
                ChannelMessage.channel_target_id == channel.id,
                ChannelMessage.message_id == snapshot.message_id,
            )
        )
        published_at = normalize_datetime(snapshot.published_at)
        if existing:
            existing.content_preview = snapshot.content_preview or existing.content_preview
            existing.message_url = snapshot.message_url or existing.message_url or channel_message_url(channel, snapshot.message_id)
            existing.comment_available = bool(snapshot.comment_available)
            existing.published_at = published_at or existing.published_at
            continue
        session.add(
            ChannelMessage(
                tenant_id=task.tenant_id,
                channel_target_id=channel.id,
                message_id=snapshot.message_id,
                message_url=snapshot.message_url or channel_message_url(channel, snapshot.message_id),
                content_preview=snapshot.content_preview,
                comment_available=bool(snapshot.comment_available),
                published_at=published_at,
            )
        )
        created += 1
    if created:
        session.flush()
    if snapshots:
        task.last_error = ""
    return created


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
    account_ids_by_message = {message.id: set() for message in messages}
    if not account_ids_by_message:
        return account_ids_by_message
    message_by_channel_id = {message.id: message.id for message in messages}
    message_by_remote_id = {message.message_id: message.id for message in messages if message.message_id is not None}
    statuses = ["pending", "executing", "success"] if execution_date else ["pending", "executing", "success", "failed"]
    if include_skipped_codes:
        statuses.append("skipped")
    for account_id, payload, status, result in session.execute(
        select(Action.account_id, Action.payload, Action.status, Action.result).where(
            Action.task_id == task.id,
            Action.action_type == action_type,
            Action.account_id.is_not(None),
            Action.status.in_(statuses),
        )
    ):
        if not isinstance(payload, dict):
            continue
        if status == "skipped" and not _skipped_code_matches(result, include_skipped_codes):
            continue
        if execution_date and str(payload.get("execution_date") or "") != execution_date:
            continue
        message_ids = {
            message_by_channel_id.get(payload.get("channel_message_id")),
            message_by_remote_id.get(payload.get("message_id")),
        }
        for message_id in message_ids - {None}:
            account_ids_by_message[message_id].add(int(account_id))
    return account_ids_by_message


def _skipped_code_matches(result: dict | None, include_skipped_codes: set[str] | None) -> bool:
    if not include_skipped_codes or not isinstance(result, dict):
        return False
    return str(result.get("error_code") or "") in include_skipped_codes


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


def channel_messages(session: Session, tenant_id: int, config: dict, *, comment_available_only: bool = False) -> list[ChannelMessage]:
    stmt = select(ChannelMessage).where(ChannelMessage.tenant_id == tenant_id, ChannelMessage.channel_target_id == int(config.get("target_channel_id") or 0))
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
