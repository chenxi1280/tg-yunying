from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, AiUsageLedger, ChannelMessage, ContentKeywordRule, GroupArchive, GroupContextMessage, OperationTarget, RuleSet, RuleSetVersion, Task, TgAccount, TgGroup, TgGroupAccount
from app.schemas.operations_center import (
    ListenerAccountOut,
    ListenerSnapshotOut,
    ListenerSummaryOut,
    ListenerTaskOut,
    MetricBucketOut,
    OperationMetricsOut,
    RuleCenterSummaryOut,
    RuleSetCreate,
    RuleSetOut,
    RuleSetVersionCreate,
    RuleSummaryOut,
    RuleTestHitOut,
    RuleTestOut,
)
from app.services._common import _now, audit


ACTIVE_TASK_STATUSES = {"draft", "pending", "running", "paused"}
LISTENER_TASK_STATUSES = {"pending", "running"}

SYSTEM_RULES = [
    RuleSummaryOut(
        key="auto-validation",
        category="自动校验",
        name="AI 内容发送前校验",
        status="已启用",
        detail="空内容、敏感词、重复内容、长度、外链、@ 成员、账号冷却和目标频控检查",
        version="system",
    ),
    RuleSummaryOut(
        key="relay-routing",
        category="路由策略",
        name="转发监听自动路由",
        status="已启用",
        detail="源消息先过滤和转换，再按任务目标群与账号策略生成发送项",
        version="system",
    ),
    RuleSummaryOut(
        key="sticky-account",
        category="发送账号策略",
        name="目标粘性账号优先",
        status="已启用",
        detail="账号可重复发送，但受冷却、每日上限、目标群连续发送限制约束",
        version="system",
    ),
    RuleSummaryOut(
        key="retry-policy",
        category="失败处理",
        name="失败重试与跳过策略",
        status="已启用",
        detail="失败项可重试，账号不可用、内容拦截、上下文过期等原因保留在执行记录",
        version="system",
    ),
]


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
        listener_accounts = _listener_accounts_for_object(session, tenant_id, subscriber_tasks, object_type="channel", object_id=channel.id)
        items.append(
            ListenerSnapshotOut(
                key=f"channel:{channel.id}",
                object_type="channel",
                title=channel.title,
                peer_id=channel.tg_peer_id,
                status="聚合监听中",
                listener_account_count=len(listener_accounts),
                subscriber_task_count=len(task_ids),
                event_backlog_count=_task_backlog_count(session, tenant_id, task_ids),
                last_event_at=_iso(_channel_last_event_at(session, tenant_id, channel.id) or channel.last_sync_at),
                task_ids=task_ids,
                listener_accounts=listener_accounts,
                subscriber_tasks=[_listener_task_out(task) for task in subscriber_tasks],
            )
        )
    for group in groups:
        subscriber_tasks = [task for task in tasks if _task_uses_group(task, group.id)]
        if not subscriber_tasks:
            continue
        task_ids = [task.id for task in subscriber_tasks]
        listener_accounts = _listener_accounts_for_object(session, tenant_id, subscriber_tasks, object_type="group", object_id=group.id)
        items.append(
            ListenerSnapshotOut(
                key=f"group:{group.id}",
                object_type="group",
                title=group.title,
                peer_id=group.tg_peer_id,
                status="聚合监听中",
                listener_account_count=len(listener_accounts),
                subscriber_task_count=len(task_ids),
                event_backlog_count=_task_backlog_count(session, tenant_id, task_ids),
                last_event_at=_iso(_group_last_event_at(session, tenant_id, group.id) or group.listener_last_polled_at),
                last_error=group.listener_last_error or "",
                task_ids=task_ids,
                listener_accounts=listener_accounts,
                subscriber_tasks=[_listener_task_out(task) for task in subscriber_tasks],
            )
        )
    return ListenerSummaryOut(
        channel_count=sum(1 for item in items if item.object_type == "channel"),
        group_count=sum(1 for item in items if item.object_type == "group"),
        subscriber_task_count=sum(item.subscriber_task_count for item in items),
        items=items,
    )


def rule_center_summary(session: Session, tenant_id: int) -> RuleCenterSummaryOut:
    keyword_rules = list(session.scalars(select(ContentKeywordRule).where(ContentKeywordRule.tenant_id == tenant_id)))
    rule_sets = list_rule_sets(session, tenant_id)
    relay_tasks = [
        task
        for task in _active_tasks(session, tenant_id)
        if task.type == "group_relay"
    ]
    items = [
        *SYSTEM_RULES,
        *[_rule_set_summary(rule_set) for rule_set in rule_sets],
        *[_keyword_rule_summary(rule) for rule in keyword_rules],
        *[_relay_task_rule_summary(task) for task in relay_tasks],
    ]
    return RuleCenterSummaryOut(
        system_rule_count=len(SYSTEM_RULES),
        keyword_rule_count=len(keyword_rules),
        relay_task_rule_count=len(relay_tasks),
        items=items,
    )


def operation_metrics_summary(session: Session, tenant_id: int) -> OperationMetricsOut:
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    today_end = today_start + timedelta(days=1)

    total_accounts = _count(session, TgAccount, TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None))
    online_accounts = _count(session, TgAccount, TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None), TgAccount.status == "在线")
    abnormal_accounts = _count(
        session,
        TgAccount,
        TgAccount.tenant_id == tenant_id,
        TgAccount.deleted_at.is_(None),
        TgAccount.status.in_(["异常", "需重新登录", "受限", "禁用", "离线"]),
    )
    avg_health = session.scalar(
        select(func.coalesce(func.avg(TgAccount.health_score), 0)).where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
        )
    ) or 0

    target_filters = [OperationTarget.tenant_id == tenant_id]
    total_targets = _count(session, OperationTarget, *target_filters)
    sendable_targets = _count(session, OperationTarget, *target_filters, OperationTarget.can_send.is_(True))
    channel_targets = _count(session, OperationTarget, *target_filters, OperationTarget.target_type == "channel")
    group_targets = _count(session, OperationTarget, *target_filters, OperationTarget.target_type == "group")

    total_actions = _count(session, Action, Action.tenant_id == tenant_id)
    sent_actions = _count(session, Action, Action.tenant_id == tenant_id, Action.action_type == "send_message", Action.status == "success")
    today_sent_actions = _count(
        session,
        Action,
        Action.tenant_id == tenant_id,
        Action.action_type == "send_message",
        Action.status == "success",
        Action.executed_at >= today_start,
        Action.executed_at < today_end,
    )
    failed_actions = _count(session, Action, Action.tenant_id == tenant_id, Action.status == "failed")
    skipped_actions = _count(session, Action, Action.tenant_id == tenant_id, Action.status == "skipped")

    channel_actions = _count(session, Action, Action.tenant_id == tenant_id, Action.action_type.in_(["view_message", "like_message", "post_comment"]))
    channel_success = _count(session, Action, Action.tenant_id == tenant_id, Action.action_type.in_(["view_message", "like_message", "post_comment"]), Action.status == "success")
    channel_failed = _count(session, Action, Action.tenant_id == tenant_id, Action.action_type.in_(["view_message", "like_message", "post_comment"]), Action.status == "failed")

    ai_tasks = _count(session, Task, Task.tenant_id == tenant_id, Task.type == "group_ai_chat", Task.deleted_at.is_(None))
    ai_turns = _count(session, Action, Action.tenant_id == tenant_id, Action.task_type == "group_ai_chat", Action.action_type == "send_message")
    ai_sent_turns = _count(session, Action, Action.tenant_id == tenant_id, Action.task_type == "group_ai_chat", Action.status == "success")

    relay_tasks = _count(session, Task, Task.tenant_id == tenant_id, Task.type == "group_relay", Task.deleted_at.is_(None))
    relay_items = _count(session, Action, Action.tenant_id == tenant_id, Action.task_type == "group_relay", Action.action_type == "send_message")
    relay_sent = _count(session, Action, Action.tenant_id == tenant_id, Action.task_type == "group_relay", Action.status == "success")
    relay_failed = _count(session, Action, Action.tenant_id == tenant_id, Action.task_type == "group_relay", Action.status == "failed")

    archive_count = _count(session, GroupArchive, GroupArchive.tenant_id == tenant_id)
    archived_messages = session.scalar(select(func.coalesce(func.sum(GroupArchive.message_count), 0)).where(GroupArchive.tenant_id == tenant_id)) or 0
    archived_members = session.scalar(select(func.coalesce(func.sum(GroupArchive.member_count), 0)).where(GroupArchive.tenant_id == tenant_id)) or 0

    ai_requests = _count(session, AiUsageLedger, AiUsageLedger.tenant_id == tenant_id)
    ai_tokens = session.scalar(select(func.coalesce(func.sum(AiUsageLedger.total_tokens), 0)).where(AiUsageLedger.tenant_id == tenant_id)) or 0
    ai_cost = session.scalar(select(func.coalesce(func.sum(AiUsageLedger.total_cost), 0)).where(AiUsageLedger.tenant_id == tenant_id)) or 0

    return OperationMetricsOut(
        accounts=[
            _metric("accounts.total", "账号总数", total_accounts, "已接入且未删除的 TG 账号"),
            _metric("accounts.online", "在线账号", online_accounts, "可参与发送或监听的账号"),
            _metric("accounts.abnormal", "异常账号", abnormal_accounts, "离线、受限、需重新登录或禁用"),
            _metric("accounts.health", "平均健康分", round(float(avg_health), 1), "账号健康分布均值"),
        ],
        targets=[
            _metric("targets.total", "目标总数", total_targets, "已纳入运营目标中心"),
            _metric("targets.sendable", "可发送目标", sendable_targets, "当前标记为可发送"),
            _metric("targets.groups", "群聊目标", group_targets, "群聊/讨论组目标"),
            _metric("targets.channels", "频道目标", channel_targets, "频道目标"),
        ],
        messages=[
            _metric("messages.total_actions", "执行项总数", total_actions, "任务中心生成的所有执行项"),
            _metric("messages.sent", "发送成功", sent_actions, "消息发送、AI 活跃、转发监听成功发送"),
            _metric("messages.today_sent", "今日发送", today_sent_actions, "今日 00:00 后成功发送"),
            _metric("messages.success_rate", "发送成功率", f"{_rate(sent_actions, sent_actions + failed_actions)}%", "按成功/失败发送项计算"),
        ],
        channel_interactions=[
            _metric("channel.total", "频道互动项", channel_actions, "浏览、点赞、评论/回复执行项"),
            _metric("channel.success", "互动完成", channel_success, "频道互动成功数"),
            _metric("channel.failed", "互动失败", channel_failed, "频道互动失败数"),
        ],
        ai_activity=[
            _metric("ai_activity.tasks", "活跃任务", ai_tasks, "AI 活跃群任务数"),
            _metric("ai_activity.turns", "Turn 数", ai_turns, "AI 活跃群发言执行项"),
            _metric("ai_activity.sent", "已发送发言", ai_sent_turns, "AI 自动校验后成功发送"),
        ],
        relay=[
            _metric("relay.tasks", "转发任务", relay_tasks, "转发监听任务数"),
            _metric("relay.items", "转发发送项", relay_items, "源事件过滤/转换/路由后的目标群发送项"),
            _metric("relay.sent", "转发成功", relay_sent, "已成功发送到目标群"),
            _metric("relay.failed", "转发失败", relay_failed, "发送失败或规则异常后的失败项"),
        ],
        archives=[
            _metric("archives.tasks", "归档任务", archive_count, "群归档记录数"),
            _metric("archives.messages", "归档消息", int(archived_messages), "累计采集消息数"),
            _metric("archives.members", "归档成员", int(archived_members), "累计采集成员数"),
        ],
        ai_usage=[
            _metric("ai_usage.requests", "AI 请求", ai_requests, "AI 调用记录数"),
            _metric("ai_usage.tokens", "Token", int(ai_tokens), "输入输出累计 Token"),
            _metric("ai_usage.cost", "费用", round(float(ai_cost), 6), "按模型价格累计"),
        ],
        failures=[
            _metric("failures.actions", "失败执行项", failed_actions, "需要重试、换号或排查"),
            _metric("failures.skipped", "跳过执行项", skipped_actions, "自动校验、上下文过期或规则过滤跳过"),
            _metric("failures.rate", "失败率", f"{_rate(failed_actions, total_actions)}%", "按全部执行项计算"),
        ],
    )


def list_rule_sets(session: Session, tenant_id: int) -> list[RuleSetOut]:
    rule_sets = list(session.scalars(select(RuleSet).where(RuleSet.tenant_id == tenant_id).order_by(RuleSet.id.asc())))
    if not rule_sets:
        return []
    versions = list(
        session.scalars(
            select(RuleSetVersion)
            .where(RuleSetVersion.tenant_id == tenant_id, RuleSetVersion.rule_set_id.in_([item.id for item in rule_sets]))
            .order_by(RuleSetVersion.rule_set_id.asc(), RuleSetVersion.version.desc())
        )
    )
    by_set: dict[int, list[RuleSetVersion]] = {}
    for version in versions:
        by_set.setdefault(version.rule_set_id, []).append(version)
    return [_rule_set_out(rule_set, by_set.get(rule_set.id, [])) for rule_set in rule_sets]


def create_rule_set(session: Session, tenant_id: int, payload: RuleSetCreate, actor: str) -> RuleSetOut:
    if session.scalar(select(RuleSet.id).where(RuleSet.tenant_id == tenant_id, RuleSet.name == payload.name).limit(1)):
        raise ValueError("同名规则集已存在")
    rule_set = RuleSet(tenant_id=tenant_id, name=payload.name, description=payload.description, status="active")
    session.add(rule_set)
    session.flush()
    version = _new_rule_set_version(session, tenant_id, rule_set.id, 1, payload, actor)
    rule_set.active_version_id = version.id
    version.status = "published"
    version.published_by = actor
    version.published_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="创建规则集", target_type="rule_set", target_id=str(rule_set.id), detail=rule_set.name)
    session.commit()
    session.refresh(rule_set)
    return _rule_set_out(rule_set, [version])


def create_rule_set_version(session: Session, tenant_id: int, rule_set_id: int, payload: RuleSetVersionCreate, actor: str) -> RuleSetOut:
    rule_set = _get_rule_set(session, tenant_id, rule_set_id)
    latest = session.scalar(select(RuleSetVersion.version).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()).limit(1)) or 0
    version = _new_rule_set_version(session, tenant_id, rule_set.id, int(latest) + 1, payload, actor)
    audit(session, tenant_id=tenant_id, actor=actor, action="创建规则集版本", target_type="rule_set", target_id=str(rule_set.id), detail=f"v{version.version}")
    session.commit()
    session.refresh(rule_set)
    return _rule_set_out(rule_set, list(session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()))))


def publish_rule_set_version(session: Session, tenant_id: int, rule_set_id: int, version_id: int, actor: str) -> RuleSetOut:
    rule_set = _get_rule_set(session, tenant_id, rule_set_id)
    version = session.get(RuleSetVersion, version_id)
    if not version or version.tenant_id != tenant_id or version.rule_set_id != rule_set.id:
        raise ValueError("规则集版本不存在")
    for old_version in session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id, RuleSetVersion.status == "published")):
        old_version.status = "archived"
    version.status = "published"
    version.published_by = actor
    version.published_at = _now()
    version.updated_at = _now()
    rule_set.active_version_id = version.id
    rule_set.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="发布规则集版本", target_type="rule_set", target_id=str(rule_set.id), detail=f"v{version.version}")
    session.commit()
    session.refresh(rule_set)
    return _rule_set_out(rule_set, list(session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()))))


def test_rules(session: Session, tenant_id: int, text: str) -> RuleTestOut:
    hits = [
        RuleTestHitOut(rule_id=rule.id, keyword=rule.keyword, match_type=rule.match_type, note=rule.note or "")
        for rule in session.scalars(
            select(ContentKeywordRule).where(
                ContentKeywordRule.tenant_id == tenant_id,
                ContentKeywordRule.is_active.is_(True),
            )
        )
        if _keyword_matches(rule, text)
    ]
    return RuleTestOut(result="命中规则，进入后续拦截/转换判断" if hits else "未命中关键词规则", hits=hits)


def _metric(key: str, label: str, value: int | float | str, detail: str = "", status: str = "") -> MetricBucketOut:
    return MetricBucketOut(key=key, label=label, value=value, detail=detail, status=status)


def _count(session: Session, model, *conditions) -> int:
    return int(session.scalar(select(func.count(model.id)).where(*conditions)) or 0)


def _rate(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round(float(numerator) / float(denominator) * 100, 1)


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
            history_fetch_account_id = _as_int(config.get("history_fetch_account_id"))
            if history_fetch_account_id:
                add_accounts([history_fetch_account_id], task, "历史采集账号")
        elif task.type == "group_relay":
            monitor_account_ids = _as_int_list(config.get("monitor_account_ids"))
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
    limit = max(1, _as_int(account_config.get("max_concurrent")) or 20)
    base_conditions = [TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None)]

    if mode == "manual":
        account_ids = _as_int_list(account_config.get("account_ids"))
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
        pool_id = _as_int(account_config.get("account_group_id"))
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


def _listener_task_out(task: Task) -> ListenerTaskOut:
    return ListenerTaskOut(id=task.id, name=task.name, type=task.type, status=task.status)


def _task_account_role(task: Task) -> str:
    return {
        "channel_view": "浏览账号",
        "channel_like": "点赞账号",
        "channel_comment": "评论账号",
    }.get(task.type, "参与账号")


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_int_list(value: Any) -> list[int]:
    if not value:
        return []
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    items: list[int] = []
    for item in raw_items:
        parsed = _as_int(item)
        if parsed is not None and parsed not in items:
            items.append(parsed)
    return items


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


def _keyword_rule_summary(rule: ContentKeywordRule) -> RuleSummaryOut:
    return RuleSummaryOut(
        key=f"keyword:{rule.id}",
        category="关键词/敏感词",
        name=rule.keyword,
        status="已启用" if rule.is_active else "禁用",
        detail=f"{rule.match_type}{f' / {rule.note}' if rule.note else ''}",
        version=f"#{rule.id}",
        source="keyword",
        metadata={"rule_id": rule.id, "match_type": rule.match_type},
    )


def _rule_set_summary(rule_set: RuleSetOut) -> RuleSummaryOut:
    active = next((version for version in rule_set.versions if version.id == rule_set.active_version_id), None)
    return RuleSummaryOut(
        key=f"rule-set:{rule_set.id}",
        category="系统级规则集",
        name=rule_set.name,
        status=rule_set.status,
        detail=f"当前版本 v{active.version if active else '-'} / {rule_set.description or '过滤、转换、路由、账号策略、限速、重试'}",
        version=f"v{active.version if active else '-'}",
        source="rule_set",
        metadata={"rule_set_id": rule_set.id, "active_version_id": rule_set.active_version_id},
    )


def _relay_task_rule_summary(task: Task) -> RuleSummaryOut:
    config: dict[str, Any] = task.type_config or {}
    filters = config.get("filters") or {}
    return RuleSummaryOut(
        key=f"task:{task.id}",
        category="任务绑定规则",
        name=task.name,
        status=task.status,
        detail=f"过滤 {filters} / 转换 {config.get('content_mode') or 'raw'} / 去重 {config.get('dedup_method') or 'hash'}",
        version=task.id[:8],
        source="task",
        metadata={"task_id": task.id, "task_type": task.type},
    )


def _keyword_matches(rule: ContentKeywordRule, text: str) -> bool:
    if not text or not rule.keyword:
        return False
    if rule.match_type == "regex":
        try:
            return bool(re.search(rule.keyword, text, flags=re.IGNORECASE))
        except re.error:
            return False
    return rule.keyword.lower() in text.lower()


def _iso(value: datetime | str | None) -> str | None:
    if isinstance(value, str):
        return value
    return value.isoformat() if value else None


def _get_rule_set(session: Session, tenant_id: int, rule_set_id: int) -> RuleSet:
    rule_set = session.get(RuleSet, rule_set_id)
    if not rule_set or rule_set.tenant_id != tenant_id:
        raise ValueError("规则集不存在")
    return rule_set


def _new_rule_set_version(session: Session, tenant_id: int, rule_set_id: int, version: int, payload: RuleSetVersionCreate, actor: str) -> RuleSetVersion:
    row = RuleSetVersion(
        tenant_id=tenant_id,
        rule_set_id=rule_set_id,
        version=version,
        status="draft",
        filters=payload.filters,
        transforms=payload.transforms,
        routing=payload.routing,
        account_strategy=payload.account_strategy,
        rate_limits=payload.rate_limits,
        retry_policy=payload.retry_policy,
        created_by=actor,
    )
    session.add(row)
    session.flush()
    return row


def _rule_set_out(rule_set: RuleSet, versions: list[RuleSetVersion]) -> RuleSetOut:
    return RuleSetOut(
        id=rule_set.id,
        tenant_id=rule_set.tenant_id,
        name=rule_set.name,
        description=rule_set.description,
        status=rule_set.status,
        active_version_id=rule_set.active_version_id,
        versions=[
            {
                "id": version.id,
                "tenant_id": version.tenant_id,
                "rule_set_id": version.rule_set_id,
                "version": version.version,
                "status": version.status,
                "filters": version.filters or {},
                "transforms": version.transforms or {},
                "routing": version.routing or {},
                "account_strategy": version.account_strategy or {},
                "rate_limits": version.rate_limits or {},
                "retry_policy": version.retry_policy or {},
                "created_by": version.created_by,
                "published_by": version.published_by,
                "published_at": _iso(version.published_at),
                "created_at": _iso(version.created_at) or "",
                "updated_at": _iso(version.updated_at) or "",
            }
            for version in versions
        ],
        created_at=_iso(rule_set.created_at) or "",
        updated_at=_iso(rule_set.updated_at) or "",
    )


__all__ = [
    "create_rule_set",
    "create_rule_set_version",
    "listener_summary",
    "list_rule_sets",
    "operation_metrics_summary",
    "publish_rule_set_version",
    "rule_center_summary",
    "test_rules",
]
