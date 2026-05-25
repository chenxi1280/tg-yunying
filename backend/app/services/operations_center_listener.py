from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelMessage,
    GroupContextMessage,
    ListenerSourceState,
    MessageFingerprint,
    OperationTarget,
    Task,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.schemas.operations_center import (
    ListenerAccountOut,
    ListenerErrorOut,
    ListenerEventOut,
    ListenerSnapshotOut,
    ListenerSummaryOut,
    ListenerTaskOut,
)
from app.services._common import audit
from app.services.operations_center_defaults import ACTIVE_TASK_STATUSES, LISTENER_TASK_STATUSES
from app.services.operations_center_utils import as_int, as_int_list, iso


def listener_summary(session: Session, tenant_id: int) -> ListenerSummaryOut:
    tasks = _active_tasks(session, tenant_id, statuses=LISTENER_TASK_STATUSES)
    channels = list(
        session.scalars(
            select(OperationTarget).where(
                OperationTarget.tenant_id == tenant_id,
                OperationTarget.target_type == "channel",
            )
        )
    )
    groups = list(session.scalars(select(TgGroup).where(TgGroup.tenant_id == tenant_id)))
    items: list[ListenerSnapshotOut] = []
    for channel in channels:
        subscriber_tasks = [task for task in tasks if _task_uses_channel(task, channel.id)]
        if not subscriber_tasks:
            continue
        task_ids = [task.id for task in subscriber_tasks]
        backlog_count = _task_backlog_count(session, tenant_id, task_ids)
        listener_accounts = _listener_accounts_for_object(session, tenant_id, subscriber_tasks, object_type="channel", object_id=channel.id)
        backup_account = _backup_account_for_listener(session, tenant_id, listener_accounts)
        switch_recommended, switch_reason = _listener_switch_state(listener_accounts, "", backup_account)
        items.append(
            ListenerSnapshotOut(
                key=f"channel:{channel.id}",
                object_type="channel",
                title=channel.title,
                peer_id=channel.tg_peer_id,
                status="聚合监听中",
                listener_account_count=len(listener_accounts),
                subscriber_task_count=len(task_ids),
                event_backlog_count=backlog_count,
                pending_distribution_count=backlog_count,
                dedup_event_count=_listener_dedup_event_count(session, tenant_id, "channel", channel.id),
                subscription_event_types=_listener_subscription_event_types(subscriber_tasks, "channel"),
                last_event_at=iso(_channel_last_event_at(session, tenant_id, channel.id) or channel.last_sync_at),
                backup_account=backup_account,
                switch_recommended=switch_recommended,
                switch_reason=switch_reason,
                task_ids=task_ids,
                listener_accounts=listener_accounts,
                subscriber_tasks=[_listener_task_out(task) for task in subscriber_tasks],
                recent_events=_channel_recent_events(session, tenant_id, channel.id),
            )
        )
    for group in groups:
        subscriber_tasks = [task for task in tasks if _task_uses_group(task, group.id)]
        if not subscriber_tasks:
            continue
        task_ids = [task.id for task in subscriber_tasks]
        backlog_count = _task_backlog_count(session, tenant_id, task_ids)
        listener_accounts = _listener_accounts_for_object(session, tenant_id, subscriber_tasks, object_type="group", object_id=group.id)
        backup_account = _backup_group_account_for_listener(session, tenant_id, group.id, listener_accounts)
        switch_recommended, switch_reason = _listener_switch_state(listener_accounts, group.listener_last_error or "", backup_account)
        items.append(
            ListenerSnapshotOut(
                key=f"group:{group.id}",
                object_type="group",
                title=group.title,
                peer_id=group.tg_peer_id,
                status="聚合监听中",
                listener_account_count=len(listener_accounts),
                subscriber_task_count=len(task_ids),
                event_backlog_count=backlog_count,
                pending_distribution_count=backlog_count,
                dedup_event_count=_listener_dedup_event_count(session, tenant_id, "group", group.id),
                subscription_event_types=_listener_subscription_event_types(subscriber_tasks, "group"),
                last_event_at=iso(_group_last_event_at(session, tenant_id, group.id) or group.listener_last_polled_at),
                last_error=group.listener_last_error or "",
                backup_account=backup_account,
                switch_recommended=switch_recommended,
                switch_reason=switch_reason,
                task_ids=task_ids,
                listener_accounts=listener_accounts,
                subscriber_tasks=[_listener_task_out(task) for task in subscriber_tasks],
                recent_events=_group_recent_events(session, tenant_id, group.id),
            )
        )
    return ListenerSummaryOut(
        channel_count=sum(1 for item in items if item.object_type == "channel"),
        group_count=sum(1 for item in items if item.object_type == "group"),
        subscriber_task_count=sum(item.subscriber_task_count for item in items),
        items=items,
    )


def switch_listener_account(session: Session, tenant_id: int, object_type: str, object_id: int, backup_account_id: int | None, actor: str) -> ListenerSummaryOut:
    if object_type == "channel":
        return _switch_channel_listener_account(session, tenant_id, object_id, backup_account_id, actor)
    if object_type != "group":
        raise ValueError("监听对象类型不支持切换")
    group = session.get(TgGroup, object_id)
    if not group or group.tenant_id != tenant_id:
        raise ValueError("监听对象不存在")
    current_links = list(
        session.scalars(
            select(TgGroupAccount)
            .where(TgGroupAccount.tenant_id == tenant_id, TgGroupAccount.group_id == group.id)
            .order_by(TgGroupAccount.id.asc())
        )
    )
    current_listener_accounts = [
        account
        for account in (
            session.get(TgAccount, link.account_id)
            for link in current_links
            if link.is_listener
        )
        if account and account.deleted_at is None
    ]
    listener_rows = [_listener_account_out(account) for account in current_listener_accounts]
    requested_backup = session.get(TgAccount, backup_account_id) if backup_account_id else None
    backup = _listener_account_out(requested_backup, roles=["备用监听账号"]) if requested_backup else _backup_group_account_for_listener(session, tenant_id, group.id, listener_rows)
    if not backup:
        raise ValueError("没有可切换的备用监听账号")
    backup_account = session.get(TgAccount, backup.id)
    if not backup_account or backup_account.tenant_id != tenant_id or backup_account.deleted_at is not None or backup_account.status != "在线":
        raise ValueError("备用监听账号不可用")
    backup_link = next((link for link in current_links if link.account_id == backup_account.id), None)
    if not backup_link or not backup_link.can_send:
        raise ValueError("备用监听账号未加入该群或不可发言")
    disabled_ids: list[int] = []
    for link in current_links:
        if not link.is_listener:
            continue
        account = session.get(TgAccount, link.account_id)
        if not account or account.deleted_at is not None or account.status != "在线":
            link.is_listener = False
            disabled_ids.append(link.account_id)
    backup_link.is_listener = True
    group.listener_enabled = True
    group.listener_last_error = ""
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="切换监听备用账号",
        target_type="tg_group",
        target_id=str(group.id),
        detail=f"backup_account={backup_account.id}; disabled={disabled_ids}",
    )
    session.commit()
    return listener_summary(session, tenant_id)


def list_listener_events(session: Session, tenant_id: int, object_type: str, object_id: int, *, limit: int = 50) -> list[ListenerEventOut]:
    _listener_peer(session, tenant_id, object_type, object_id)
    safe_limit = min(200, max(1, limit))
    if object_type == "channel":
        return _channel_recent_events(session, tenant_id, object_id, limit=safe_limit)
    if object_type == "group":
        return _group_recent_events(session, tenant_id, object_id, limit=safe_limit)
    raise ValueError("监听对象类型不支持")


def list_listener_errors(session: Session, tenant_id: int, object_type: str, object_id: int) -> list[ListenerErrorOut]:
    peer_id = _listener_peer(session, tenant_id, object_type, object_id)
    errors: list[ListenerErrorOut] = []
    if object_type == "group":
        group = session.get(TgGroup, object_id)
        if group and group.listener_last_error:
            errors.append(
                ListenerErrorOut(
                    id=f"group:{group.id}:last_error",
                    object_type="group",
                    object_id=group.id,
                    source_peer_id=group.tg_peer_id,
                    source="listener_object",
                    error_message=group.listener_last_error,
                    occurred_at=iso(group.listener_last_polled_at),
                )
            )
    for state in _listener_source_states(session, tenant_id, object_type, peer_id):
        if not state.last_error:
            continue
        account = session.get(TgAccount, state.account_id) if state.account_id else None
        errors.append(
            ListenerErrorOut(
                id=state.id,
                object_type=object_type,  # type: ignore[arg-type]
                object_id=object_id,
                source_peer_id=state.source_peer_id,
                account_id=state.account_id,
                account_display=account.display_name if account else "",
                source="source_state",
                error_message=state.last_error,
                last_remote_message_id=state.last_remote_message_id,
                occurred_at=iso(state.updated_at),
            )
        )
    return errors


def reset_listener_watermark(session: Session, tenant_id: int, object_type: str, object_id: int, *, reason: str, actor: str, confirm_text: str) -> ListenerSummaryOut:
    if (confirm_text or "").strip() != "确认重置":
        raise ValueError("请输入确认重置")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("请填写重置原因")
    peer_id = _listener_peer(session, tenant_id, object_type, object_id)
    states = _listener_source_states(session, tenant_id, object_type, peer_id)
    for state in states:
        state.last_remote_message_id = ""
        state.last_event_at = None
        state.backfill_until = None
        state.last_error = ""
    target_type = "operation_target"
    if object_type == "group":
        group = session.get(TgGroup, object_id)
        if group:
            group.listener_last_polled_at = None
            group.listener_last_error = ""
            target_type = "tg_group"
    elif object_type == "channel":
        channel = session.get(OperationTarget, object_id)
        if channel:
            channel.last_sync_at = None
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="重置监听水位",
        target_type=target_type,
        target_id=str(object_id),
        detail=f"object_type={object_type}; peer_id={peer_id}; states={len(states)}; reason={reason}",
    )
    session.commit()
    return listener_summary(session, tenant_id)


def _switch_channel_listener_account(session: Session, tenant_id: int, object_id: int, backup_account_id: int | None, actor: str) -> ListenerSummaryOut:
    channel = session.get(OperationTarget, object_id)
    if not channel or channel.tenant_id != tenant_id or channel.target_type != "channel":
        raise ValueError("频道监听对象不存在")
    subscriber_tasks = [task for task in _active_tasks(session, tenant_id, statuses=LISTENER_TASK_STATUSES) if _task_uses_channel(task, channel.id)]
    if not subscriber_tasks:
        raise ValueError("频道没有可切换的监听任务")
    listener_accounts = _listener_accounts_for_object(session, tenant_id, subscriber_tasks, object_type="channel", object_id=channel.id)
    requested_backup = session.get(TgAccount, backup_account_id) if backup_account_id else None
    backup = _listener_account_out(requested_backup, roles=["备用账号"]) if requested_backup else _backup_account_for_listener(session, tenant_id, listener_accounts)
    if not backup:
        raise ValueError("没有可切换的备用监听账号")
    backup_account = session.get(TgAccount, backup.id)
    if not backup_account or backup_account.tenant_id != tenant_id or backup_account.deleted_at is not None or backup_account.status != "在线":
        raise ValueError("备用监听账号不可用")

    disabled_ids: set[int] = set()
    updated_task_ids: list[str] = []
    for task in subscriber_tasks:
        account_config = dict(task.account_config or {})
        current_ids = _configured_task_account_ids(session, tenant_id, account_config)
        if not current_ids:
            continue
        current_accounts = _accounts_by_id(session, tenant_id, current_ids)
        next_ids = [
            account_id
            for account_id in current_ids
            if current_accounts.get(account_id) and current_accounts[account_id].status == "在线"
        ]
        disabled_ids.update(account_id for account_id in current_ids if account_id not in next_ids)
        if backup_account.id not in next_ids:
            next_ids.append(backup_account.id)
        if next_ids != current_ids or account_config.get("selection_mode") != "manual":
            account_config["selection_mode"] = "manual"
            account_config["account_ids"] = next_ids
            task.account_config = account_config
            updated_task_ids.append(task.id)
    if not updated_task_ids:
        raise ValueError("没有需要更新的频道监听任务")
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="切换频道监听备用账号",
        target_type="operation_target",
        target_id=str(channel.id),
        detail=f"backup_account={backup_account.id}; disabled={sorted(disabled_ids)}; tasks={updated_task_ids}",
    )
    session.commit()
    return listener_summary(session, tenant_id)


def _active_tasks(session: Session, tenant_id: int, *, statuses: set[str] | None = None) -> list[Task]:
    return list(
        session.scalars(
            select(Task).where(
                Task.tenant_id == tenant_id,
                Task.deleted_at.is_(None),
                Task.status.in_(statuses or ACTIVE_TASK_STATUSES),
            )
        )
    )


def _task_uses_channel(task: Task, target_id: int) -> bool:
    return task.type in {"channel_view", "channel_like", "channel_comment"} and int((task.type_config or {}).get("target_channel_id") or 0) == target_id


def _task_uses_group(task: Task, group_id: int) -> bool:
    config = task.type_config or {}
    if task.type == "group_ai_chat":
        return int(config.get("target_group_id") or 0) == group_id
    if task.type != "group_relay":
        return False
    return any(int(item.get("group_id") or 0) == group_id and item.get("is_active", True) for item in config.get("source_groups") or [])


def _listener_accounts_for_object(session: Session, tenant_id: int, tasks: list[Task], *, object_type: str, object_id: int) -> list[ListenerAccountOut]:
    account_roles: dict[int, list[str]] = {}
    account_task_ids: dict[int, list[str]] = {}
    account_rows: dict[int, TgAccount] = {}

    def add_accounts(account_ids: list[int], task: Task, role: str) -> None:
        accounts = _accounts_by_id(session, tenant_id, account_ids)
        for account_id in account_ids:
            account = accounts.get(account_id)
            if not account:
                continue
            account_rows.setdefault(account_id, account)
            account_roles.setdefault(account_id, [])
            account_task_ids.setdefault(account_id, [])
            if role not in account_roles[account_id]:
                account_roles[account_id].append(role)
            if task.id not in account_task_ids[account_id]:
                account_task_ids[account_id].append(task.id)

    for task in tasks:
        config = task.type_config or {}
        if object_type == "channel":
            add_accounts(_configured_task_account_ids(session, tenant_id, task.account_config or {}), task, _task_account_role(task))
            continue
        if task.type == "group_ai_chat":
            add_accounts(_configured_task_account_ids(session, tenant_id, task.account_config or {}, target_group_id=object_id), task, "发言账号")
            history_fetch_account_id = as_int(config.get("history_fetch_account_id"))
            if history_fetch_account_id:
                add_accounts([history_fetch_account_id], task, "历史采集账号")
        elif task.type == "group_relay":
            monitor_account_ids = as_int_list(config.get("monitor_account_ids"))
            if monitor_account_ids:
                add_accounts(monitor_account_ids, task, "监听账号")
            else:
                add_accounts(_group_listener_candidate_account_ids(session, tenant_id, object_id), task, "监听账号")

    return [
        ListenerAccountOut(
            id=account.id,
            display_name=account.display_name,
            username=account.username,
            status=account.status,
            roles=account_roles.get(account.id, []),
            task_ids=account_task_ids.get(account.id, []),
        )
        for account in account_rows.values()
    ]


def _configured_task_account_ids(session: Session, tenant_id: int, account_config: dict, *, target_group_id: int | None = None) -> list[int]:
    mode = account_config.get("selection_mode") or ("manual" if account_config.get("account_ids") else "all")
    limit = max(1, as_int(account_config.get("max_concurrent")) or 20)
    base_conditions = [TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None)]

    if mode == "manual":
        account_ids = as_int_list(account_config.get("account_ids"))
        if not account_ids:
            return []
        stmt = select(TgAccount.id).where(*base_conditions, TgAccount.id.in_(account_ids))
        if target_group_id:
            stmt = stmt.join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id).where(
                TgGroupAccount.group_id == target_group_id,
                TgGroupAccount.can_send.is_(True),
            )
        valid_ids = set(session.scalars(stmt))
        return [account_id for account_id in account_ids if account_id in valid_ids]

    stmt = select(TgAccount.id).where(*base_conditions).order_by(TgAccount.health_score.desc(), TgAccount.id.asc())
    if mode == "group":
        pool_id = as_int(account_config.get("account_group_id"))
        if not pool_id:
            return []
        stmt = stmt.where(TgAccount.pool_id == pool_id)
    if target_group_id:
        stmt = stmt.join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id).where(
            TgGroupAccount.group_id == target_group_id,
            TgGroupAccount.can_send.is_(True),
        )
    return list(session.scalars(stmt.limit(limit)))


def _group_listener_candidate_account_ids(session: Session, tenant_id: int, group_id: int) -> list[int]:
    listener_ids = list(
        session.scalars(
            select(TgGroupAccount.account_id)
            .join(TgAccount, TgAccount.id == TgGroupAccount.account_id)
            .where(
                TgGroupAccount.tenant_id == tenant_id,
                TgGroupAccount.group_id == group_id,
                TgGroupAccount.is_listener.is_(True),
                TgAccount.deleted_at.is_(None),
            )
            .order_by(TgGroupAccount.id.asc())
        )
    )
    if listener_ids:
        return listener_ids
    return list(
        session.scalars(
            select(TgGroupAccount.account_id)
            .join(TgAccount, TgAccount.id == TgGroupAccount.account_id)
            .where(
                TgGroupAccount.tenant_id == tenant_id,
                TgGroupAccount.group_id == group_id,
                TgGroupAccount.can_send.is_(True),
                TgAccount.deleted_at.is_(None),
            )
            .order_by(TgGroupAccount.id.asc())
        )
    )


def _accounts_by_id(session: Session, tenant_id: int, account_ids: list[int]) -> dict[int, TgAccount]:
    if not account_ids:
        return {}
    rows = session.scalars(
        select(TgAccount).where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.id.in_(list(dict.fromkeys(account_ids))),
            TgAccount.deleted_at.is_(None),
        )
    )
    return {account.id: account for account in rows}


def _listener_account_out(account: TgAccount, *, roles: list[str] | None = None, task_ids: list[str] | None = None) -> ListenerAccountOut:
    return ListenerAccountOut(
        id=account.id,
        display_name=account.display_name,
        username=account.username,
        status=account.status,
        roles=roles or [],
        task_ids=task_ids or [],
    )


def _backup_account_for_listener(session: Session, tenant_id: int, listener_accounts: list[ListenerAccountOut]) -> ListenerAccountOut | None:
    active_listener_ids = {account.id for account in listener_accounts if account.status == "在线"}
    row = session.scalar(
        select(TgAccount)
        .where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.status == "在线",
            ~TgAccount.id.in_(active_listener_ids or {-1}),
        )
        .order_by(TgAccount.health_score.desc(), TgAccount.id.asc())
        .limit(1)
    )
    return _listener_account_out(row, roles=["备用账号"]) if row else None


def _backup_group_account_for_listener(session: Session, tenant_id: int, group_id: int, listener_accounts: list[ListenerAccountOut]) -> ListenerAccountOut | None:
    active_listener_ids = {account.id for account in listener_accounts if account.status == "在线"}
    row = session.scalar(
        select(TgAccount)
        .join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id)
        .where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.status == "在线",
            TgGroupAccount.tenant_id == tenant_id,
            TgGroupAccount.group_id == group_id,
            TgGroupAccount.can_send.is_(True),
            ~TgAccount.id.in_(active_listener_ids or {-1}),
        )
        .order_by(TgAccount.health_score.desc(), TgAccount.id.asc())
        .limit(1)
    )
    return _listener_account_out(row, roles=["备用监听账号"]) if row else None


def _listener_switch_state(listener_accounts: list[ListenerAccountOut], last_error: str, backup_account: ListenerAccountOut | None) -> tuple[bool, str]:
    active_count = sum(1 for account in listener_accounts if account.status == "在线")
    if last_error and backup_account:
        return True, f"最近监听异常：{last_error}"
    if listener_accounts and active_count == 0 and backup_account:
        return True, "当前监听账号均不在线"
    if not listener_accounts and backup_account:
        return True, "当前没有监听账号"
    if active_count <= 1 and backup_account:
        return False, "建议保留备用账号，避免单点监听"
    return False, ""


def _listener_task_out(task: Task) -> ListenerTaskOut:
    return ListenerTaskOut(id=task.id, name=task.name, type=task.type, status=task.status)


def _task_account_role(task: Task) -> str:
    return {
        "channel_view": "浏览账号",
        "channel_like": "点赞账号",
        "channel_comment": "评论账号",
    }.get(task.type, "参与账号")


def _task_backlog_count(session: Session, tenant_id: int, task_ids: list[str]) -> int:
    if not task_ids:
        return 0
    return int(
        session.scalar(
            select(func.count(Action.id)).where(
                Action.tenant_id == tenant_id,
                Action.task_id.in_(task_ids),
                Action.status.in_(["pending", "executing"]),
            )
        )
        or 0
    )


def _listener_subscription_event_types(tasks: list[Task], object_type: str) -> list[str]:
    labels: list[str] = []
    for task in tasks:
        next_labels: list[str]
        if object_type == "channel":
            next_labels = {
                "channel_view": ["频道消息"],
                "channel_like": ["频道消息", "Reaction"],
                "channel_comment": ["频道消息", "评论/回复"],
            }.get(task.type, ["频道事件"])
        elif task.type == "group_ai_chat":
            next_labels = ["群上下文", "真实用户活跃"]
        elif task.type == "group_relay":
            next_labels = ["源群新消息", "规则分发"]
        else:
            next_labels = ["群事件"]
        for label in next_labels:
            if label not in labels:
                labels.append(label)
    return labels


def _listener_dedup_event_count(session: Session, tenant_id: int, object_type: str, object_id: int) -> int:
    if object_type == "channel":
        return int(
            session.scalar(
                select(func.count(func.distinct(ChannelMessage.message_id))).where(
                    ChannelMessage.tenant_id == tenant_id,
                    ChannelMessage.channel_target_id == object_id,
                )
            )
            or 0
        )
    relay_pattern = f"%:relay:{object_id}:target:%"
    group_context_count = session.scalar(
        select(func.count(func.distinct(GroupContextMessage.remote_message_id))).where(
            GroupContextMessage.tenant_id == tenant_id,
            GroupContextMessage.group_id == object_id,
        )
    ) or 0
    relay_fingerprint_count = session.scalar(
        select(func.count(MessageFingerprint.id)).where(
            MessageFingerprint.tenant_id == tenant_id,
            MessageFingerprint.source_group_id.like(relay_pattern),
        )
    ) or 0
    return int(group_context_count) + int(relay_fingerprint_count)


def _channel_last_event_at(session: Session, tenant_id: int, channel_target_id: int) -> datetime | None:
    return session.scalar(
        select(func.max(func.coalesce(ChannelMessage.published_at, ChannelMessage.created_at))).where(
            ChannelMessage.tenant_id == tenant_id,
            ChannelMessage.channel_target_id == channel_target_id,
        )
    )


def _group_last_event_at(session: Session, tenant_id: int, group_id: int) -> datetime | None:
    return session.scalar(
        select(func.max(GroupContextMessage.sent_at)).where(
            GroupContextMessage.tenant_id == tenant_id,
            GroupContextMessage.group_id == group_id,
        )
    )


def _listener_peer(session: Session, tenant_id: int, object_type: str, object_id: int) -> str:
    if object_type == "group":
        group = session.get(TgGroup, object_id)
        if not group or group.tenant_id != tenant_id:
            raise ValueError("监听对象不存在")
        return group.tg_peer_id
    if object_type == "channel":
        channel = session.get(OperationTarget, object_id)
        if not channel or channel.tenant_id != tenant_id or channel.target_type != "channel":
            raise ValueError("频道监听对象不存在")
        return channel.tg_peer_id
    raise ValueError("监听对象类型不支持")


def _listener_source_states(session: Session, tenant_id: int, object_type: str, peer_id: str) -> list[ListenerSourceState]:
    return list(
        session.scalars(
            select(ListenerSourceState)
            .where(
                ListenerSourceState.tenant_id == tenant_id,
                ListenerSourceState.source_type == object_type,
                ListenerSourceState.source_peer_id == peer_id,
            )
            .order_by(ListenerSourceState.updated_at.desc())
        )
    )


def _channel_recent_events(session: Session, tenant_id: int, channel_target_id: int, *, limit: int = 5) -> list[ListenerEventOut]:
    rows = list(
        session.scalars(
            select(ChannelMessage)
            .where(ChannelMessage.tenant_id == tenant_id, ChannelMessage.channel_target_id == channel_target_id)
            .order_by(ChannelMessage.published_at.desc().nullslast(), ChannelMessage.created_at.desc())
            .limit(limit)
        )
    )
    return [
        ListenerEventOut(
            id=item.id,
            event_type="channel_message",
            content=item.content_preview or item.message_url or f"频道消息 #{item.message_id}",
            remote_message_id=str(item.message_id),
            occurred_at=iso(item.published_at or item.created_at),
        )
        for item in rows
    ]


def _group_recent_events(session: Session, tenant_id: int, group_id: int, *, limit: int = 5) -> list[ListenerEventOut]:
    rows = list(
        session.scalars(
            select(GroupContextMessage)
            .where(GroupContextMessage.tenant_id == tenant_id, GroupContextMessage.group_id == group_id)
            .order_by(GroupContextMessage.sent_at.desc().nullslast(), GroupContextMessage.created_at.desc())
            .limit(limit)
        )
    )
    return [
        ListenerEventOut(
            id=item.id,
            event_type=item.message_type or "group_message",
            content=item.content,
            account_id=item.listener_account_id,
            sender_name=item.sender_name,
            sender_peer_id=item.sender_peer_id,
            sender_username=item.sender_username,
            sender_role=item.sender_role,
            is_bot=item.is_bot,
            remote_message_id=item.remote_message_id,
            occurred_at=iso(item.sent_at or item.created_at),
        )
        for item in rows
    ]


__all__ = [
    "list_listener_errors",
    "list_listener_events",
    "listener_summary",
    "reset_listener_watermark",
    "switch_listener_account",
]
