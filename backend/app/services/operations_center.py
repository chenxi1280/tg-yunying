from __future__ import annotations

import csv
import re
from io import StringIO
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, AiUsageLedger, ChannelMessage, ContentKeywordRule, GroupArchive, GroupContextMessage, MessageFingerprint, OperationTarget, RuleSet, RuleSetVersion, SchedulingSetting, Task, TgAccount, TgGroup, TgGroupAccount, WorkerHeartbeat
from app.schemas.operations_center import (
    ListenerAccountOut,
    ListenerEventOut,
    ListenerSnapshotOut,
    ListenerSummaryOut,
    ListenerTaskOut,
    MetricBucketOut,
    OperationMetricDetailOut,
    OperationMetricsOut,
    RelayAttributionReportOut,
    RelayMaterialAttributionOut,
    RuleConflictOut,
    RuleCenterSummaryOut,
    RuleConversionMetricOut,
    RuleCrossMetricOut,
    RuleDimensionMetricOut,
    RuleExecutionMetricOut,
    RuleSetCreate,
    RuleSetBoundTaskOut,
    RuleSetOut,
    RuleSetVersionCreate,
    RuleSummaryOut,
    RuleTestCandidateOut,
    RuleTestHitOut,
    RuleTestOut,
    RuleTestRouteOut,
    RuleTrendMetricOut,
)
from app.services._common import _as_utc, _now, audit
from app.services.rule_engine import apply_output_policy, evaluate_input_filter, task_type_labels
from app.services.task_center.executors.group_relay import apply_transform_rules, relay_filter_expression_reason, resolve_relay_target_ids
from app.services.task_center.fingerprints import content_fingerprint


ACTIVE_TASK_STATUSES = {"draft", "pending", "running", "paused"}
LISTENER_TASK_STATUSES = {"pending", "running"}
DEFAULT_RULE_SET_NAME = "默认运营规则集"
LEGACY_DEFAULT_RELAY_RULE_SET_NAME = "默认转发监听过滤规则"
DEFAULT_RULE_SET_DESCRIPTION = "系统初始化的通用规则集，默认不拦截内容，可用于监听转发、AI 回复、AI 评论和普通消息发送。"
DEFAULT_RULE_TASK_TYPES = ["group_relay", "group_ai_chat", "channel_comment", "message_send"]
DEFAULT_RELAY_FILTERS = {
    "keyword_whitelist": [],
    "keyword_blacklist": [],
    "min_message_length": None,
    "max_message_length": None,
    "allowed_media_types": [],
    "blocked_user_ids": [],
    "only_with_media": False,
    "only_text": False,
    "language_filter": None,
}
DEFAULT_RELAY_OUTPUT_CHECKS = {
    "forbidden_keywords": [],
    "forbid_links": False,
    "forbid_mentions": True,
    "max_length": None,
    "failure_strategy": "transform_once_drop",
}


def _default_relay_output_checks() -> dict[str, Any]:
    return {
        key: list(value) if isinstance(value, list) else value
        for key, value in DEFAULT_RELAY_OUTPUT_CHECKS.items()
    }


def _default_relay_filters() -> dict[str, Any]:
    return {
        key: list(value) if isinstance(value, list) else value
        for key, value in DEFAULT_RELAY_FILTERS.items()
    }

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
                last_event_at=_iso(_channel_last_event_at(session, tenant_id, channel.id) or channel.last_sync_at),
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
                last_event_at=_iso(_group_last_event_at(session, tenant_id, group.id) or group.listener_last_polled_at),
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


def relay_attribution_csv(session: Session, tenant_id: int, *, limit: int = 5000) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "action_id",
            "task_id",
            "task_name",
            "relay_batch_id",
            "relay_event_id",
            "source_event_key",
            "source_operation_target_id",
            "target_operation_target_id",
            "account_id",
            "rule_set_id",
            "rule_set_version_id",
            "status",
            "retry_count",
            "material_fingerprint",
            "source_info",
            "original_text",
            "transformed_text",
            "error_code",
            "error_message",
            "scheduled_at",
            "executed_at",
        ]
    )
    rows = session.execute(
        select(Action, Task)
        .join(Task, Task.id == Action.task_id)
        .where(Task.tenant_id == tenant_id, Task.type == "group_relay")
        .order_by(Action.created_at.desc())
        .limit(max(1, min(int(limit or 5000), 20000)))
    ).all()
    for action, task in rows:
        payload = action.payload if isinstance(action.payload, dict) else {}
        batch_id = str(payload.get("relay_batch_id") or "")
        if not batch_id:
            continue
        result = action.result if isinstance(action.result, dict) else {}
        original_text = str(payload.get("original_text") or "")
        transformed_text = str(payload.get("message_text") or "")
        material_text = original_text or transformed_text
        relay_event_id = str(payload.get("relay_event_id") or "")
        source_id = payload.get("source_operation_target_id") or payload.get("source_group_id") or "-"
        writer.writerow(
            [
                action.id,
                task.id,
                task.name,
                batch_id,
                relay_event_id,
                f"{source_id}:{relay_event_id or '-'}",
                payload.get("source_operation_target_id") or "",
                payload.get("operation_target_id") or "",
                action.account_id or "",
                payload.get("rule_set_id") or "",
                payload.get("rule_set_version_id") or "",
                action.status,
                int(action.retry_count or 0),
                content_fingerprint(material_text) if material_text else "",
                str(payload.get("source_info") or ""),
                original_text,
                transformed_text,
                str(result.get("error_code") or ""),
                str(result.get("error_message") or ""),
                action.scheduled_at.isoformat() if action.scheduled_at else "",
                action.executed_at.isoformat() if action.executed_at else "",
            ]
        )
    return output.getvalue()


def relay_attribution_report(session: Session, tenant_id: int, *, limit: int = 5000) -> RelayAttributionReportOut:
    rows = session.execute(
        select(Action, Task)
        .join(Task, Task.id == Action.task_id)
        .where(Task.tenant_id == tenant_id, Task.type == "group_relay")
        .order_by(Action.created_at.desc())
        .limit(max(1, min(int(limit or 5000), 20000)))
    ).all()
    metrics: dict[str, dict[str, Any]] = {}
    for action, task in rows:
        payload = action.payload if isinstance(action.payload, dict) else {}
        batch_id = str(payload.get("relay_batch_id") or "")
        if not batch_id:
            continue
        original_text = str(payload.get("original_text") or "")
        transformed_text = str(payload.get("message_text") or "")
        material_text = original_text or transformed_text
        fingerprint = str(payload.get("material_fingerprint") or "") or (content_fingerprint(material_text) if material_text else "")
        if not fingerprint:
            fingerprint = f"batch:{batch_id}"
        metric = metrics.setdefault(
            fingerprint,
            {
                "sample_text": material_text[:160],
                "task_ids": set(),
                "source_events": set(),
                "targets": set(),
                "accounts": set(),
                "action_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "pending_count": 0,
                "retry_count": 0,
                "last_used_at": None,
            },
        )
        metric["sample_text"] = metric["sample_text"] or material_text[:160]
        metric["task_ids"].add(task.id)
        source_event = str(payload.get("source_event_key") or payload.get("relay_event_id") or "")
        if source_event:
            metric["source_events"].add(source_event)
        target_id = payload.get("operation_target_id") or payload.get("target_group_id")
        if target_id:
            metric["targets"].add(str(target_id))
        if action.account_id:
            metric["accounts"].add(str(action.account_id))
        metric["action_count"] += 1
        metric["retry_count"] += int(action.retry_count or 0)
        if action.status == "success":
            metric["success_count"] += 1
        elif action.status == "failed":
            metric["failed_count"] += 1
        elif action.status == "skipped":
            metric["skipped_count"] += 1
        elif action.status in {"pending", "executing"}:
            metric["pending_count"] += 1
        occurred_at = action.executed_at or action.scheduled_at or action.created_at
        if occurred_at and (metric["last_used_at"] is None or occurred_at > metric["last_used_at"]):
            metric["last_used_at"] = occurred_at
    report_rows: list[RelayMaterialAttributionOut] = []
    for fingerprint, metric in metrics.items():
        action_count = int(metric["action_count"] or 0)
        success_count = int(metric["success_count"] or 0)
        report_rows.append(
            RelayMaterialAttributionOut(
                key=fingerprint,
                material_fingerprint=fingerprint,
                sample_text=str(metric["sample_text"] or ""),
                task_count=len(metric["task_ids"]),
                source_event_count=len(metric["source_events"]),
                target_count=len(metric["targets"]),
                account_count=len(metric["accounts"]),
                action_count=action_count,
                success_count=success_count,
                failed_count=int(metric["failed_count"] or 0),
                skipped_count=int(metric["skipped_count"] or 0),
                pending_count=int(metric["pending_count"] or 0),
                retry_count=int(metric["retry_count"] or 0),
                success_rate=round(success_count * 100 / action_count, 2) if action_count else 0,
                last_used_at=_iso(metric["last_used_at"]),
            )
        )
    report_rows.sort(key=lambda item: (item.action_count, item.last_used_at or ""), reverse=True)
    return RelayAttributionReportOut(
        total_materials=len(report_rows),
        total_source_events=sum(item.source_event_count for item in report_rows),
        total_actions=sum(item.action_count for item in report_rows),
        rows=report_rows,
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


def rule_center_summary(session: Session, tenant_id: int) -> RuleCenterSummaryOut:
    rule_sets = list_rule_sets(session, tenant_id)
    relay_tasks = [
        task
        for task in _active_tasks(session, tenant_id)
        if task.type == "group_relay"
    ]
    items = [
        *SYSTEM_RULES,
        *[_rule_set_summary(rule_set) for rule_set in rule_sets],
        *[_relay_task_rule_summary(task) for task in relay_tasks],
    ]
    target_metrics, account_metrics, keyword_metrics = _rule_dimension_metrics(session, tenant_id)
    return RuleCenterSummaryOut(
        system_rule_count=len(SYSTEM_RULES),
        keyword_rule_count=0,
        relay_task_rule_count=len(relay_tasks),
        items=items,
        conflicts=_rule_conflicts([], rule_sets, relay_tasks),
        execution_metrics=_rule_execution_metrics(session, tenant_id),
        target_metrics=target_metrics,
        account_metrics=account_metrics,
        keyword_metrics=keyword_metrics,
        trend_metrics=_rule_trend_metrics(session, tenant_id),
        conversion_metrics=_rule_conversion_metrics(session, tenant_id),
        cross_metrics=_rule_cross_metrics(session, tenant_id),
    )


def operation_metrics_summary(session: Session, tenant_id: int) -> OperationMetricsOut:
    today_start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
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
        risk_control=_risk_control_metrics(session, tenant_id),
        account_details=_account_metric_details(session, tenant_id),
        target_details=_target_metric_details(session, tenant_id),
        task_details=_task_metric_details(session, tenant_id),
        failure_details=_failure_metric_details(session, tenant_id),
        risk_details=_risk_control_details(session, tenant_id),
    )


def list_rule_sets(session: Session, tenant_id: int) -> list[RuleSetOut]:
    _ensure_default_rule_set(session, tenant_id)
    rule_sets = list(session.scalars(select(RuleSet).where(RuleSet.tenant_id == tenant_id).order_by(RuleSet.id.asc())))
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


def _ensure_default_rule_set(session: Session, tenant_id: int) -> RuleSet:
    existing = session.scalar(
        select(RuleSet).where(
            RuleSet.tenant_id == tenant_id,
            RuleSet.name == DEFAULT_RULE_SET_NAME,
        )
    )
    if not existing:
        existing = session.scalar(
            select(RuleSet).where(
                RuleSet.tenant_id == tenant_id,
                RuleSet.name == LEGACY_DEFAULT_RELAY_RULE_SET_NAME,
            )
        )
    if existing:
        changed = False
        if existing.name == LEGACY_DEFAULT_RELAY_RULE_SET_NAME:
            existing.name = DEFAULT_RULE_SET_NAME
            changed = True
        if existing.description != DEFAULT_RULE_SET_DESCRIPTION:
            existing.description = DEFAULT_RULE_SET_DESCRIPTION
            changed = True
        if set(existing.task_types or []) != set(DEFAULT_RULE_TASK_TYPES):
            existing.task_types = DEFAULT_RULE_TASK_TYPES
            changed = True
        default_policy = dict(existing.default_policy or {})
        if default_policy.get("version_binding") != "follow_current":
            default_policy["version_binding"] = "follow_current"
            existing.default_policy = default_policy
            changed = True
        versions = list(session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == existing.id).order_by(RuleSetVersion.version.desc())))
        if not versions:
            version = RuleSetVersion(
                tenant_id=tenant_id,
                rule_set_id=existing.id,
                version=1,
                status="published",
                filters=_default_relay_filters(),
                output_checks=_default_relay_output_checks(),
                transforms={},
                routing={},
                account_strategy={},
                rate_limits={},
                retry_policy={},
                created_by="system",
                published_by="system",
                published_at=_now(),
            )
            session.add(version)
            session.flush()
            existing.active_version_id = version.id
            changed = True
        elif not existing.active_version_id:
            published = next((item for item in versions if item.status == "published"), versions[0])
            existing.active_version_id = published.id
            if published.status != "published":
                published.status = "published"
                published.published_by = published.published_by or "system"
                published.published_at = published.published_at or _now()
            changed = True
        if changed:
            existing.updated_at = _now()
            session.commit()
            session.refresh(existing)
        return existing
    rule_set = RuleSet(
        tenant_id=tenant_id,
        name=DEFAULT_RULE_SET_NAME,
        description=DEFAULT_RULE_SET_DESCRIPTION,
        status="active",
        task_types=DEFAULT_RULE_TASK_TYPES,
        default_policy={"input_failure": "skip", "output_failure": "transform_once_drop", "version_binding": "follow_current"},
    )
    session.add(rule_set)
    session.flush()
    version = RuleSetVersion(
        tenant_id=tenant_id,
        rule_set_id=rule_set.id,
        version=1,
        status="published",
        filters=_default_relay_filters(),
        output_checks=_default_relay_output_checks(),
        transforms={},
        routing={},
        account_strategy={},
        rate_limits={},
        retry_policy={},
        created_by="system",
        published_by="system",
        published_at=_now(),
    )
    session.add(version)
    session.flush()
    rule_set.active_version_id = version.id
    rule_set.updated_at = _now()
    audit(
        session,
        tenant_id=tenant_id,
        actor="system",
        action="初始化默认运营规则集",
        target_type="rule_set",
        target_id=str(rule_set.id),
        detail=rule_set.name,
    )
    session.commit()
    session.refresh(rule_set)
    return rule_set


def create_rule_set(session: Session, tenant_id: int, payload: RuleSetCreate, actor: str) -> RuleSetOut:
    if session.scalar(select(RuleSet.id).where(RuleSet.tenant_id == tenant_id, RuleSet.name == payload.name).limit(1)):
        raise ValueError("同名规则集已存在")
    rule_set = RuleSet(
        tenant_id=tenant_id,
        name=payload.name,
        description=payload.description,
        status="active",
        task_types=payload.task_types,
        default_policy=payload.default_policy,
    )
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


def update_rule_set_config(session: Session, tenant_id: int, rule_set_id: int, payload: RuleSetVersionCreate, actor: str) -> RuleSetOut:
    rule_set = _get_rule_set(session, tenant_id, rule_set_id)
    latest = session.scalar(select(RuleSetVersion.version).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()).limit(1)) or 0
    version = _new_rule_set_version(session, tenant_id, rule_set.id, int(latest) + 1, payload, actor)
    for old_version in session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id, RuleSetVersion.status == "published")):
        old_version.status = "archived"
        old_version.updated_at = _now()
    version.status = "published"
    version.published_by = actor
    version.published_at = _now()
    version.updated_at = _now()
    rule_set.active_version_id = version.id
    rule_set.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="更新规则集配置并发布", target_type="rule_set", target_id=str(rule_set.id), detail=f"v{version.version}")
    session.commit()
    session.refresh(rule_set)
    return _rule_set_out(rule_set, list(session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()))))


def copy_rule_set_version(session: Session, tenant_id: int, rule_set_id: int, version_id: int, actor: str) -> RuleSetOut:
    rule_set = _get_rule_set(session, tenant_id, rule_set_id)
    source = _get_rule_set_version(session, tenant_id, rule_set.id, version_id)
    latest = session.scalar(select(RuleSetVersion.version).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()).limit(1)) or 0
    payload = _version_payload_from_row(source, version_note=f"复制自 v{source.version}")
    version = _new_rule_set_version(session, tenant_id, rule_set.id, int(latest) + 1, payload, actor)
    audit(session, tenant_id=tenant_id, actor=actor, action="复制规则集版本为草稿", target_type="rule_set", target_id=str(rule_set.id), detail=f"v{source.version}->v{version.version}")
    session.commit()
    session.refresh(rule_set)
    return _rule_set_out(rule_set, list(session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()))))


def publish_rule_set_version(session: Session, tenant_id: int, rule_set_id: int, version_id: int, actor: str) -> RuleSetOut:
    rule_set = _get_rule_set(session, tenant_id, rule_set_id)
    version = _get_rule_set_version(session, tenant_id, rule_set.id, version_id)
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


def rollback_rule_set_version(session: Session, tenant_id: int, rule_set_id: int, version_id: int, actor: str) -> RuleSetOut:
    rule_set = _get_rule_set(session, tenant_id, rule_set_id)
    source = _get_rule_set_version(session, tenant_id, rule_set.id, version_id)
    latest = session.scalar(select(RuleSetVersion.version).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()).limit(1)) or 0
    payload = _version_payload_from_row(source, version_note=f"回滚自 v{source.version}")
    version = _new_rule_set_version(session, tenant_id, rule_set.id, int(latest) + 1, payload, actor)
    for old_version in session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id, RuleSetVersion.status == "published")):
        old_version.status = "archived"
    version.status = "published"
    version.published_by = actor
    version.published_at = _now()
    version.updated_at = _now()
    rule_set.active_version_id = version.id
    rule_set.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="回滚规则集版本", target_type="rule_set", target_id=str(rule_set.id), detail=f"v{source.version}->v{version.version}")
    session.commit()
    session.refresh(rule_set)
    return _rule_set_out(rule_set, list(session.scalars(select(RuleSetVersion).where(RuleSetVersion.rule_set_id == rule_set.id).order_by(RuleSetVersion.version.desc()))))


def list_rule_set_bound_tasks(session: Session, tenant_id: int, rule_set_id: int) -> list[RuleSetBoundTaskOut]:
    rule_set = _get_rule_set(session, tenant_id, rule_set_id)
    version_ids = {
        version_id
        for version_id in session.scalars(select(RuleSetVersion.id).where(RuleSetVersion.tenant_id == tenant_id, RuleSetVersion.rule_set_id == rule_set.id))
    }
    tasks = list(session.scalars(select(Task).where(Task.tenant_id == tenant_id, Task.deleted_at.is_(None)).order_by(Task.updated_at.desc())))
    rows: list[RuleSetBoundTaskOut] = []
    for task in tasks:
        config = task.type_config or {}
        config_rule_set_id = _as_int(config.get("rule_set_id"))
        config_version_id = _as_int(config.get("rule_set_version_id"))
        if config_rule_set_id != rule_set.id and config_version_id not in version_ids:
            continue
        resolved = config_version_id or rule_set.active_version_id
        rows.append(
            RuleSetBoundTaskOut(
                id=task.id,
                name=task.name,
                type=task.type,
                status=task.status,
                binding_mode="fixed_version" if config_version_id else "follow_current",
                rule_set_id=config_rule_set_id or rule_set.id,
                rule_set_version_id=config_version_id,
                resolved_rule_set_version_id=resolved,
                created_at=_iso(task.created_at) or "",
                updated_at=_iso(task.updated_at) or "",
            )
        )
    return rows


def test_rules(
    session: Session,
    tenant_id: int,
    text: str,
    test_type: str = "group_relay",
    test_mode: str = "rules_only",
    candidates: list[str] | None = None,
    context: str = "",
    rule_set_version_id: int | None = None,
    source_group_id: int | None = None,
    sender_id: str = "",
    message_type: str = "text",
) -> RuleTestOut:
    version = session.get(RuleSetVersion, rule_set_version_id) if rule_set_version_id else None
    if version and version.tenant_id != tenant_id:
        version = None
    rule_set = session.get(RuleSet, version.rule_set_id) if version else None
    if version:
        filters = dict(version.filters or {})
        input_text = "\n".join(item for item in [context, text] if item)
        input_result = evaluate_input_filter(input_text, sender_id, message_type or "text", filters)
        filter_passed = input_result.passed
        filter_reason = "" if filter_passed else input_result.reason or _relay_filter_reason(input_text, sender_id, message_type or "text", filters)
        transformed_text = apply_transform_rules(text, dict(version.transforms or {})) if filter_passed else text
        output_candidates: list[RuleTestCandidateOut] = []
        if test_type in {"group_ai_chat", "channel_comment", "message_send"}:
            raw_candidates = candidates or ([text] if text else [])
            for index, candidate in enumerate(raw_candidates, start=1):
                policy_result = apply_output_policy(candidate, version.output_checks or {}, version.transforms or {})
                output_candidates.append(
                    RuleTestCandidateOut(
                        index=index,
                        original_text=candidate,
                        passed=policy_result.allowed,
                        action=policy_result.action,
                        reason=policy_result.reason,
                        transformed_text=policy_result.content,
                    )
                )
        route_config = {
            "routing": dict(version.routing or {}),
            "target_group_ids": (version.routing or {}).get("target_group_ids") or (version.routing or {}).get("default_target_group_ids") or [],
            "account_strategy": dict(version.account_strategy or {}),
        }
        source_id = int(source_group_id or 0)
        target_ids = resolve_relay_target_ids(route_config, source_id, transformed_text)
        routes = [_rule_test_route(session, tenant_id, target_id, version.account_strategy or {}) for target_id in target_ids]
        target_summary = "、".join(route.title for route in routes) if routes else "未解析到目标群，请检查 routing.target_group_ids / routes / keyword_routes"
        account_summary = _rule_test_account_summary(routes, version.account_strategy or {})
        rate_summary = _rule_test_rate_summary(version.rate_limits or {})
        block_reasons: list[str] = []
        if not filter_passed:
            block_reasons.append(filter_reason or "未通过规则集过滤")
        return RuleTestOut(
            result="规则版本预览：通过过滤，已计算转换和路由" if filter_passed else "规则版本预览：未通过过滤",
            test_mode=test_mode,
            is_test_data=True,
            hits=[],
            input_hits=input_result.hits,
            output_candidates=output_candidates,
            should_block=bool(block_reasons),
            block_reason="；".join(block_reasons),
            filter_passed=filter_passed,
            filter_reason=filter_reason,
            rule_set_version_id=version.id,
            rule_set_name=rule_set.name if rule_set else "",
            transformed_text=transformed_text,
            target_summary=target_summary,
            target_routes=routes,
            account_strategy=account_summary,
            rate_limit_summary=rate_summary,
        )
    return RuleTestOut(
        result="未命中规则条件",
        test_mode=test_mode,
        is_test_data=True,
        hits=[],
        should_block=False,
        block_reason="",
        transformed_text=text,
        target_summary="规则测试只验证内容规则；实际目标由任务绑定的路由配置决定",
        account_strategy="规则测试不占用账号；执行时按任务账号池、冷却和目标粘性策略选择",
        rate_limit_summary="测试不触发限流；执行时按账号冷却、小时/日上限与失败重试策略校验",
    )


def _metric(key: str, label: str, value: int | float | str, detail: str = "", status: str = "") -> MetricBucketOut:
    return MetricBucketOut(key=key, label=label, value=value, detail=detail, status=status)


def _relay_filter_reason(text: str, sender_id: str, message_type: str, filters: dict[str, Any]) -> str:
    lowered = (text or "").lower()
    whitelist = [str(item).lower() for item in filters.get("keyword_whitelist") or [] if str(item).strip()]
    if whitelist and not any(item in lowered for item in whitelist):
        return f"未命中白名单关键词：{', '.join(whitelist)}"
    blacklist = [str(item).lower() for item in filters.get("keyword_blacklist") or [] if str(item).strip()]
    blocked = [item for item in blacklist if item in lowered]
    if blocked:
        return f"命中黑名单关键词：{', '.join(blocked)}"
    if filters.get("min_message_length") and len(text or "") < int(filters["min_message_length"]):
        return f"内容长度低于最小值 {filters['min_message_length']}"
    if filters.get("max_message_length") and len(text or "") > int(filters["max_message_length"]):
        return f"内容长度超过最大值 {filters['max_message_length']}"
    if sender_id and sender_id in {str(item) for item in filters.get("blocked_user_ids") or []}:
        return f"发送者 {sender_id} 在屏蔽列表"
    allowed = {str(item) for item in filters.get("allowed_media_types") or []}
    if allowed and message_type not in allowed:
        return f"消息类型 {message_type or 'text'} 不在允许列表"
    is_text = message_type in {"text", "文本", ""}
    if filters.get("only_with_media") and is_text:
        return "规则要求带媒体消息"
    if filters.get("only_text") and not is_text:
        return "规则只允许文本消息"
    expression_reason = relay_filter_expression_reason(text, sender_id, message_type, filters)
    if expression_reason:
        return expression_reason
    return "未通过过滤规则"


def _rule_test_route(session: Session, tenant_id: int, target_id: int, account_strategy: dict[str, Any]) -> RuleTestRouteOut:
    group = session.get(TgGroup, target_id)
    if not group or group.tenant_id != tenant_id:
        return RuleTestRouteOut(group_id=target_id, title=f"群 #{target_id}", status="不存在", account_strategy=_account_strategy_label(account_strategy))
    account_count = session.scalar(
        select(func.count(TgGroupAccount.id))
        .join(TgAccount, TgAccount.id == TgGroupAccount.account_id)
        .where(
            TgGroupAccount.tenant_id == tenant_id,
            TgGroupAccount.group_id == target_id,
            TgGroupAccount.can_send.is_(True),
            TgAccount.status == "在线",
            TgAccount.deleted_at.is_(None),
        )
    ) or 0
    status = "可发送" if group.can_send and account_count else "缺少可发送账号" if group.can_send else "目标不可发送"
    return RuleTestRouteOut(
        group_id=group.id,
        title=group.title,
        status=status,
        can_send_account_count=int(account_count),
        account_strategy=_account_strategy_label(account_strategy),
    )


def _rule_test_account_summary(routes: list[RuleTestRouteOut], strategy: dict[str, Any]) -> str:
    label = _account_strategy_label(strategy)
    if not routes:
        return f"{label}；未解析到目标群，无法预估账号"
    total = sum(route.can_send_account_count for route in routes)
    missing = [route.title for route in routes if route.can_send_account_count <= 0]
    suffix = f"；{len(routes)} 个目标共 {total} 个可发送账号"
    if missing:
        suffix += f"；缺账号：{'、'.join(missing)}"
    return f"{label}{suffix}"


def _account_strategy_label(strategy: dict[str, Any]) -> str:
    mode = str(strategy.get("mode") or "target_sticky")
    labels = {
        "fixed": "固定账号",
        "target_sticky": "目标群粘性账号",
        "target_group_sticky": "目标群粘性账号",
        "source_target_sticky": "源群+目标群粘性账号",
        "round_robin": "轮询账号",
        "random": "随机账号",
        "weighted_random": "按权重随机账号",
    }
    account_ids = strategy.get("account_ids") or strategy.get("send_account_ids")
    if strategy.get("account_id") or strategy.get("fixed_account_id"):
        return f"{labels.get(mode, mode)} #{strategy.get('account_id') or strategy.get('fixed_account_id')}"
    if account_ids:
        return f"{labels.get(mode, mode)} / 指定账号 {account_ids}"
    return labels.get(mode, mode)


def _rule_test_rate_summary(rate_limits: dict[str, Any]) -> str:
    if not rate_limits:
        return "未配置额外限速；执行时仍受账号冷却、小时/日上限和失败重试策略约束"
    parts = []
    for key, label in [
        ("per_account_per_hour", "每账号每小时"),
        ("per_account_per_day", "每账号每日"),
        ("per_target_per_hour", "每目标每小时"),
        ("cooldown_seconds", "冷却秒数"),
        ("max_concurrent", "最大并发"),
    ]:
        if rate_limits.get(key) is not None:
            parts.append(f"{label}={rate_limits[key]}")
    return "；".join(parts) if parts else f"限速配置：{rate_limits}"


def _metric_detail(key: str, title: str, category: str, status: str, detail: str = "", related_id: str = "", occurred_at: datetime | str | None = None) -> OperationMetricDetailOut:
    return OperationMetricDetailOut(key=key, title=title, category=category, status=status, detail=detail, related_id=related_id, occurred_at=_iso(occurred_at))


def _account_metric_details(session: Session, tenant_id: int) -> list[OperationMetricDetailOut]:
    rows = session.scalars(
        select(TgAccount)
        .where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.status.in_(["异常", "需重新登录", "受限", "禁用", "离线"]),
        )
        .order_by(TgAccount.health_score.asc(), TgAccount.id.asc())
        .limit(10)
    )
    return [
        _metric_detail(
            f"account:{account.id}",
            account.display_name,
            "账号异常",
            account.status,
            f"@{account.username or '-'} / 健康分 {round(float(account.health_score or 0), 1)}",
            str(account.id),
            account.last_active_at or account.created_at,
        )
        for account in rows
    ]


def _target_metric_details(session: Session, tenant_id: int) -> list[OperationMetricDetailOut]:
    rows = session.scalars(
        select(OperationTarget)
        .where(
            OperationTarget.tenant_id == tenant_id,
            (OperationTarget.can_send.is_(False)) | (OperationTarget.auth_status != "已授权运营"),
        )
        .order_by(OperationTarget.updated_at.desc(), OperationTarget.id.asc())
        .limit(10)
    )
    return [
        _metric_detail(
            f"target:{target.id}",
            target.title,
            "目标风险",
            "只读" if not target.can_send else target.auth_status,
            f"{target.target_type} / {target.tg_peer_id}{f' / @{target.username}' if target.username else ''}",
            str(target.id),
            target.updated_at,
        )
        for target in rows
    ]


def _task_metric_details(session: Session, tenant_id: int) -> list[OperationMetricDetailOut]:
    rows = session.scalars(
        select(Task)
        .where(Task.tenant_id == tenant_id, Task.deleted_at.is_(None))
        .order_by(Task.updated_at.desc(), Task.created_at.desc())
        .limit(10)
    )
    return [
        _metric_detail(
            f"task:{task.id}",
            task.name,
            task.type,
            task.status,
            _task_metric_detail_text(task),
            task.id,
            task.updated_at,
        )
        for task in rows
    ]


def _task_metric_detail_text(task: Task) -> str:
    stats = task.stats or {}
    return f"成功 {int(stats.get('success_count') or 0)} / 失败 {int(stats.get('failure_count') or 0)} / {task.last_error or '无错误'}"


def _failure_metric_details(session: Session, tenant_id: int) -> list[OperationMetricDetailOut]:
    rows = session.scalars(
        select(Action)
        .where(Action.tenant_id == tenant_id, Action.status.in_(["failed", "skipped"]))
        .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
        .limit(10)
    )
    details: list[OperationMetricDetailOut] = []
    for action in rows:
        task = session.get(Task, action.task_id)
        details.append(
            _metric_detail(
                f"action:{action.id}",
                task.name if task else action.action_type,
                action.action_type,
                action.status,
                _action_trace_detail(action),
                action.id,
                action.executed_at or action.created_at,
            )
        )
    return details


def _action_trace_detail(action: Action) -> str:
    result = action.result if isinstance(action.result, dict) else {}
    payload = action.payload if isinstance(action.payload, dict) else {}
    failure_detail = result.get("error_message") or result.get("detail") or result.get("error_code") or "无补充失败信息"
    trace_parts = [str(failure_detail)]
    if action.account_id:
        trace_parts.append(f"账号 #{action.account_id}")
    target = payload.get("target_display") or payload.get("channel_id") or payload.get("chat_id")
    if target:
        trace_parts.append(f"目标 {target}")
    relay_event_id = payload.get("relay_event_id")
    if relay_event_id:
        trace_parts.append(f"事件 {relay_event_id}")
    source_group_id = payload.get("source_group_id")
    if source_group_id:
        trace_parts.append(f"源群 #{source_group_id}")
    rule_set_id = payload.get("rule_set_id")
    rule_set_version_id = payload.get("rule_set_version_id")
    if rule_set_id or rule_set_version_id:
        trace_parts.append(f"规则集 #{rule_set_id or '-'} / 版本 #{rule_set_version_id or '-'}")
    return "；".join(trace_parts)


def _risk_control_metrics(session: Session, tenant_id: int) -> list[MetricBucketOut]:
    setting = _effective_scheduling_setting(session, tenant_id)
    keyword_total = _count(session, ContentKeywordRule, ContentKeywordRule.tenant_id == tenant_id)
    keyword_active = _count(
        session,
        ContentKeywordRule,
        ContentKeywordRule.tenant_id == tenant_id,
        ContentKeywordRule.is_active.is_(True),
    )
    groups = list(session.scalars(select(TgGroup).where(TgGroup.tenant_id == tenant_id)))
    groups_with_daily_limit = sum(1 for group in groups if int(group.daily_limit or 0) > 0)
    groups_with_banned_words = sum(1 for group in groups if (group.banned_words or "").strip())
    groups_with_link_whitelist = sum(1 for group in groups if (group.link_whitelist or "").strip())
    average_daily_limit = round(sum(int(group.daily_limit or 0) for group in groups) / len(groups), 1) if groups else 0
    tasks = _active_tasks(session, tenant_id)
    tasks_with_rate_limits = [task for task in tasks if _task_has_rate_limit(task)]
    risk_counts = _risk_action_counts(session, tenant_id)
    fingerprint_total = _count(session, MessageFingerprint, MessageFingerprint.tenant_id == tenant_id)
    today_start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    fingerprints_today = _count(
        session,
        MessageFingerprint,
        MessageFingerprint.tenant_id == tenant_id,
        MessageFingerprint.created_at >= today_start,
    )
    heartbeat_cutoff = _now() - timedelta(minutes=2)
    active_workers = _count(session, WorkerHeartbeat, WorkerHeartbeat.status == "active", WorkerHeartbeat.last_seen_at >= heartbeat_cutoff)
    stale_workers = _count(session, WorkerHeartbeat, WorkerHeartbeat.last_seen_at < heartbeat_cutoff)
    quiet_value = f"{setting.quiet_start}-{setting.quiet_end}" if setting and setting.quiet_hours_enabled else "未启用"
    quiet_detail = (
        f"{setting.quiet_timezone} / 重试 {setting.default_max_retries} 次 / "
        f"{setting.default_retry_delay_seconds}s / 内容拒绝 {setting.default_on_content_rejected} / "
        f"账号限额 {setting.default_account_hour_limit}/小时 {setting.default_account_day_limit}/日 冷却 {setting.default_account_cooldown_seconds}s"
        if setting
        else "未找到发送节奏配置"
    )
    return [
        _metric("risk.quiet_hours", "全局静默", quiet_value, quiet_detail),
        _metric("risk.keyword_rules", "敏感词规则", keyword_active, f"启用 {keyword_active} / 全部 {keyword_total}"),
        _metric(
            "risk.group_policies",
            "群风控覆盖",
            f"{groups_with_daily_limit}/{len(groups)}",
            f"禁词 {groups_with_banned_words} 个群 / 链接白名单 {groups_with_link_whitelist} 个群 / 平均日上限 {average_daily_limit}",
        ),
        _metric("risk.task_rate_limits", "任务限速", len(tasks_with_rate_limits), "配置小时/日上限、冷却或静默期的运行任务"),
        _metric("risk.content_rejected", "内容拦截", risk_counts["content"], "最近 5000 个失败/跳过执行项中的内容、关键词、链接、重复拦截"),
        _metric("risk.rate_limited", "频控拦截", risk_counts["rate"], "最近 5000 个失败/跳过执行项中的上限、冷却、FloodWait 或慢速限制"),
        _metric("risk.duplicates", "重复指纹", fingerprint_total, f"去重指纹总数 / 今日新增 {fingerprints_today}"),
        _metric("risk.worker_heartbeat", "Worker 心跳", active_workers, f"2 分钟内活跃 {active_workers} / 过期 {stale_workers}"),
    ]


def _risk_control_details(session: Session, tenant_id: int) -> list[OperationMetricDetailOut]:
    details: list[OperationMetricDetailOut] = []
    setting = _effective_scheduling_setting(session, tenant_id)
    if setting:
        details.append(
            _metric_detail(
                "risk:scheduling",
                "发送节奏与默认失败策略",
                "全局策略",
                "已启用" if setting.respect_send_window else "未强制发送窗口",
                (
                    f"抖动 {setting.jitter_min_seconds}-{setting.jitter_max_seconds}s / 批次 {setting.batch_interval_seconds}s；"
                    f"静默 {setting.quiet_start}-{setting.quiet_end}={setting.quiet_hours_enabled}；"
                    f"账号封禁 {setting.default_on_account_banned} / API 限流 {setting.default_on_api_rate_limit} / 内容拒绝 {setting.default_on_content_rejected}；"
                    f"账号全局限额 {setting.default_account_hour_limit}/小时 {setting.default_account_day_limit}/日 / 冷却 {setting.default_account_cooldown_seconds}s"
                ),
                str(setting.id),
                setting.updated_at,
            )
        )
    active_keywords = list(
        session.scalars(
            select(ContentKeywordRule)
            .where(ContentKeywordRule.tenant_id == tenant_id, ContentKeywordRule.is_active.is_(True))
            .order_by(ContentKeywordRule.id.desc())
            .limit(8)
        )
    )
    if active_keywords:
        details.append(
            _metric_detail(
                "risk:keywords",
                "启用中的敏感词规则",
                "内容拦截",
                f"{len(active_keywords)} 条",
                "、".join(rule.keyword for rule in active_keywords),
                "",
                active_keywords[0].updated_at,
            )
        )
    details.extend(_group_risk_policy_details(session, tenant_id))
    details.extend(_task_rate_limit_details(session, tenant_id))
    details.extend(_recent_risk_action_details(session, tenant_id))
    return details[:30]


def _effective_scheduling_setting(session: Session, tenant_id: int) -> SchedulingSetting | None:
    return session.scalar(select(SchedulingSetting).where(SchedulingSetting.tenant_id == tenant_id)) or session.scalar(
        select(SchedulingSetting).where(SchedulingSetting.tenant_id.is_(None))
    )


def _task_has_rate_limit(task: Task) -> bool:
    pacing = task.pacing_config or {}
    account_config = task.account_config or {}
    return any(
        [
            pacing.get("max_actions_per_hour"),
            pacing.get("max_actions_per_day"),
            pacing.get("quiet_hours"),
            account_config.get("cooldown_per_account_minutes"),
        ]
    )


def _risk_action_counts(session: Session, tenant_id: int) -> dict[str, int]:
    content = 0
    rate = 0
    for action in _recent_failed_or_skipped_actions(session, tenant_id, limit=5000):
        text = _risk_action_text(action)
        if _looks_like_content_risk(text):
            content += 1
        if _looks_like_rate_risk(text):
            rate += 1
    return {"content": content, "rate": rate}


def _group_risk_policy_details(session: Session, tenant_id: int) -> list[OperationMetricDetailOut]:
    groups = list(
        session.scalars(
            select(TgGroup)
            .where(TgGroup.tenant_id == tenant_id)
            .order_by(TgGroup.id.asc())
            .limit(10)
        )
    )
    return [
        _metric_detail(
            f"risk:group:{group.id}",
            group.title,
            "群风控",
            "可发送" if group.can_send else "不可发送",
            (
                f"日上限 {group.daily_limit} / 账号冷却 {group.account_cooldown_seconds}s / 群冷却 {group.group_cooldown_seconds}s；"
                f"禁词 {'已配置' if (group.banned_words or '').strip() else '未配置'} / "
                f"链接白名单 {'已配置' if (group.link_whitelist or '').strip() else '未配置'}"
            ),
            str(group.id),
            group.listener_last_polled_at,
        )
        for group in groups
        if (group.banned_words or "").strip()
        or (group.link_whitelist or "").strip()
        or int(group.daily_limit or 0) > 0
        or int(group.account_cooldown_seconds or 0) > 0
        or int(group.group_cooldown_seconds or 0) > 0
    ]


def _task_rate_limit_details(session: Session, tenant_id: int) -> list[OperationMetricDetailOut]:
    tasks = [task for task in _active_tasks(session, tenant_id) if _task_has_rate_limit(task)]
    details: list[OperationMetricDetailOut] = []
    for task in tasks[:10]:
        pacing = task.pacing_config or {}
        account_config = task.account_config or {}
        parts = []
        if pacing.get("max_actions_per_hour"):
            parts.append(f"每小时 {pacing['max_actions_per_hour']}")
        if pacing.get("max_actions_per_day"):
            parts.append(f"每日 {pacing['max_actions_per_day']}")
        if pacing.get("quiet_hours"):
            quiet = pacing["quiet_hours"]
            parts.append(f"静默 {quiet.get('start', '02:00')}-{quiet.get('end', '08:00')}")
        if account_config.get("cooldown_per_account_minutes") is not None:
            parts.append(f"账号冷却 {account_config.get('cooldown_per_account_minutes')} 分钟")
        details.append(
            _metric_detail(
                f"risk:task:{task.id}",
                task.name,
                "任务限速",
                task.status,
                " / ".join(parts) or "未配置额外限速",
                task.id,
                task.updated_at,
            )
        )
    heartbeats = list(session.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc()).limit(8)))
    heartbeat_cutoff = _now() - timedelta(minutes=2)
    for heartbeat in heartbeats:
        stale = _is_stale_heartbeat(heartbeat.last_seen_at, heartbeat_cutoff)
        details.append(
            _metric_detail(
                f"risk:worker:{heartbeat.worker_id}",
                f"Worker {heartbeat.process_type}",
                "进程心跳",
                "过期" if stale else heartbeat.status,
                f"{heartbeat.hostname}:{heartbeat.pid} / last_seen={heartbeat.last_seen_at.isoformat() if heartbeat.last_seen_at else ''}",
                heartbeat.worker_id,
                heartbeat.last_seen_at,
            )
        )
    return details


def _is_stale_heartbeat(last_seen_at: datetime, cutoff: datetime) -> bool:
    return _as_utc(last_seen_at) < _as_utc(cutoff)


def _recent_risk_action_details(session: Session, tenant_id: int) -> list[OperationMetricDetailOut]:
    details: list[OperationMetricDetailOut] = []
    for action in _recent_failed_or_skipped_actions(session, tenant_id, limit=20):
        text = _risk_action_text(action)
        if not (_looks_like_content_risk(text) or _looks_like_rate_risk(text)):
            continue
        category = "内容拦截" if _looks_like_content_risk(text) else "频控拦截"
        details.append(
            _metric_detail(
                f"risk:action:{action.id}",
                action.action_type,
                category,
                action.status,
                _action_trace_detail(action),
                action.id,
                action.executed_at or action.created_at,
            )
        )
    return details[:10]


def _recent_failed_or_skipped_actions(session: Session, tenant_id: int, *, limit: int) -> list[Action]:
    return list(
        session.scalars(
            select(Action)
            .where(Action.tenant_id == tenant_id, Action.status.in_(["failed", "skipped"]))
            .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
            .limit(limit)
        )
    )


def _risk_action_text(action: Action) -> str:
    result = action.result if isinstance(action.result, dict) else {}
    payload = action.payload if isinstance(action.payload, dict) else {}
    values = [
        result.get("error_message"),
        result.get("detail"),
        result.get("reason"),
        result.get("error_code"),
        payload.get("failure_detail"),
        payload.get("filter_reason"),
        payload.get("block_reason"),
        payload.get("content"),
    ]
    return " ".join(str(item) for item in values if item).lower()


def _looks_like_content_risk(text: str) -> bool:
    return any(marker in text for marker in ["内容", "关键词", "敏感", "禁词", "链接", "白名单", "重复", "过滤", "拦截", "content", "keyword", "duplicate"])


def _looks_like_rate_risk(text: str) -> bool:
    return any(marker in text for marker in ["上限", "冷却", "限流", "慢速", "flood", "rate", "slowmode", "cooldown", "quota"])


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


def _channel_recent_events(session: Session, tenant_id: int, channel_target_id: int) -> list[ListenerEventOut]:
    rows = list(
        session.scalars(
            select(ChannelMessage)
            .where(ChannelMessage.tenant_id == tenant_id, ChannelMessage.channel_target_id == channel_target_id)
            .order_by(ChannelMessage.published_at.desc().nullslast(), ChannelMessage.created_at.desc())
            .limit(5)
        )
    )
    return [
        ListenerEventOut(
            id=item.id,
            event_type="channel_message",
            content=item.content_preview or item.message_url or f"频道消息 #{item.message_id}",
            occurred_at=_iso(item.published_at or item.created_at),
        )
        for item in rows
    ]


def _group_recent_events(session: Session, tenant_id: int, group_id: int) -> list[ListenerEventOut]:
    rows = list(
        session.scalars(
            select(GroupContextMessage)
            .where(GroupContextMessage.tenant_id == tenant_id, GroupContextMessage.group_id == group_id)
            .order_by(GroupContextMessage.sent_at.desc().nullslast(), GroupContextMessage.created_at.desc())
            .limit(5)
        )
    )
    return [
        ListenerEventOut(
            id=item.id,
            event_type=item.message_type or "group_message",
            content=item.content,
            account_id=item.listener_account_id,
            sender_name=item.sender_name,
            occurred_at=_iso(item.sent_at or item.created_at),
        )
        for item in rows
    ]


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
    task_scope = " / ".join(task_type_labels(rule_set.task_types)) if rule_set.task_types else "未限定任务"
    return RuleSummaryOut(
        key=f"rule-set:{rule_set.id}",
        category="系统级规则集",
        name=rule_set.name,
        status=rule_set.status,
        detail=f"{task_scope} / 当前版本 v{active.version if active else '-'} / {rule_set.description or '过滤、输出校验、转换、路由、账号策略、限速、重试'}",
        version=f"v{active.version if active else '-'}",
        source="rule_set",
        metadata={"rule_set_id": rule_set.id, "active_version_id": rule_set.active_version_id, "task_types": rule_set.task_types},
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


def _rule_conflicts(keyword_rules: list[ContentKeywordRule], rule_sets: list[RuleSetOut], relay_tasks: list[Task]) -> list[RuleConflictOut]:
    conflicts: list[RuleConflictOut] = []
    seen_keywords: dict[tuple[str, str], list[str]] = {}
    for rule in keyword_rules:
        if not rule.is_active:
            continue
        key = ((rule.keyword or "").strip().lower(), rule.match_type or "contains")
        seen_keywords.setdefault(key, []).append(str(rule.id))
    for (keyword, match_type), ids in seen_keywords.items():
        if keyword and len(ids) > 1:
            conflicts.append(
                RuleConflictOut(
                    key=f"keyword-duplicate:{keyword}:{match_type}",
                    level="中",
                    title=f"重复关键词：{keyword}",
                    detail=f"同一匹配方式 {match_type} 下有多个启用规则，可能导致重复拦截或难以追溯。",
                    related_ids=ids,
                )
            )
    published_version_ids = {
        version.id
        for rule_set in rule_sets
        for version in rule_set.versions
        if version.status == "published"
    }
    rule_set_ids = {rule_set.id for rule_set in rule_sets}
    for rule_set in rule_sets:
        if not rule_set.active_version_id:
            conflicts.append(
                RuleConflictOut(
                    key=f"rule-set-no-active:{rule_set.id}",
                    level="高",
                    title=f"规则集未发布：{rule_set.name}",
                    detail="该规则集没有活动版本，绑定到任务后不会形成稳定规则口径。",
                    related_ids=[str(rule_set.id)],
                )
            )
    for task in relay_tasks:
        config = task.type_config or {}
        rule_set_id = _as_int(config.get("rule_set_id"))
        version_id = _as_int(config.get("rule_set_version_id"))
        if rule_set_id and rule_set_id not in rule_set_ids:
            conflicts.append(
                RuleConflictOut(
                    key=f"relay-missing-rule-set:{task.id}",
                    level="高",
                    title=f"转发任务规则集不存在：{task.name}",
                    detail=f"任务绑定的规则集 #{rule_set_id} 已不存在，执行时会退化到任务内置过滤配置。",
                    related_ids=[task.id, str(rule_set_id)],
                )
            )
        if version_id and version_id not in published_version_ids:
            conflicts.append(
                RuleConflictOut(
                    key=f"relay-missing-rule-version:{task.id}",
                    level="中",
                    title=f"转发任务规则版本未发布：{task.name}",
                    detail=f"任务绑定的规则版本 #{version_id} 不是当前发布版本，建议重新选择已发布版本。",
                    related_ids=[task.id, str(version_id)],
                )
            )
    return conflicts


def _rule_execution_metrics(session: Session, tenant_id: int) -> list[RuleExecutionMetricOut]:
    actions = list(
        session.scalars(
            select(Action)
            .where(
                Action.tenant_id == tenant_id,
                Action.task_type == "group_relay",
                Action.action_type == "send_message",
            )
            .order_by(Action.created_at.desc())
            .limit(2000)
        )
    )
    versions = {
        version.id: version
        for version in session.scalars(select(RuleSetVersion).where(RuleSetVersion.tenant_id == tenant_id))
    }
    rule_sets = {
        rule_set.id: rule_set
        for rule_set in session.scalars(select(RuleSet).where(RuleSet.tenant_id == tenant_id))
    }
    metrics: dict[str, RuleExecutionMetricOut] = {}
    task_ids: dict[str, set[str]] = {}
    for action in actions:
        payload = action.payload or {}
        version_id = _as_int(payload.get("resolved_rule_set_version_id")) or _as_int(payload.get("rule_set_version_id"))
        rule_set_id = _as_int(payload.get("rule_set_id"))
        if not version_id and not rule_set_id:
            continue
        version = versions.get(version_id or 0)
        if version and not rule_set_id:
            rule_set_id = version.rule_set_id
        rule_set = rule_sets.get(rule_set_id or 0)
        key = f"rule-version:{version_id}" if version_id else f"rule-set:{rule_set_id}"
        metric = metrics.get(key)
        if metric is None:
            metric = RuleExecutionMetricOut(
                key=key,
                rule_set_id=rule_set_id,
                rule_set_version_id=version_id,
                rule_set_name=rule_set.name if rule_set else "",
                version=version.version if version else None,
            )
            metrics[key] = metric
            task_ids[key] = set()
        metric.action_count += 1
        task_ids[key].add(action.task_id)
        if action.status == "success":
            metric.success_count += 1
        elif action.status == "failed":
            metric.failed_count += 1
        elif action.status == "skipped":
            metric.skipped_count += 1
        else:
            metric.pending_count += 1
        occurred_at = action.executed_at or action.scheduled_at or action.created_at
        if occurred_at and (metric.last_used_at is None or (_iso(occurred_at) or "") > metric.last_used_at):
            metric.last_used_at = _iso(occurred_at)
    for key, metric in metrics.items():
        metric.task_count = len(task_ids.get(key, set()))
    return sorted(metrics.values(), key=lambda item: item.last_used_at or "", reverse=True)[:100]


def _rule_dimension_metrics(session: Session, tenant_id: int) -> tuple[list[RuleDimensionMetricOut], list[RuleDimensionMetricOut], list[RuleDimensionMetricOut]]:
    actions = list(
        session.scalars(
            select(Action)
            .where(
                Action.tenant_id == tenant_id,
                Action.task_type == "group_relay",
                Action.action_type == "send_message",
            )
            .order_by(Action.created_at.desc())
            .limit(2000)
        )
    )
    target_names = {
        group.id: group.title
        for group in session.scalars(select(TgGroup).where(TgGroup.tenant_id == tenant_id))
    }
    account_names = {
        account.id: account.display_name
        for account in session.scalars(select(TgAccount).where(TgAccount.tenant_id == tenant_id))
    }
    target_metrics: dict[str, RuleDimensionMetricOut] = {}
    account_metrics: dict[str, RuleDimensionMetricOut] = {}
    keyword_metrics: dict[str, RuleDimensionMetricOut] = {}
    for action in actions:
        payload = action.payload or {}
        target_id = _as_int(payload.get("group_id"))
        if target_id:
            _touch_rule_dimension_metric(
                target_metrics,
                key=f"target:{target_id}",
                dimension="target",
                name=target_names.get(target_id, f"目标群 #{target_id}"),
                related_id=str(target_id),
                action=action,
            )
        if action.account_id:
            account_id = int(action.account_id)
            _touch_rule_dimension_metric(
                account_metrics,
                key=f"account:{account_id}",
                dimension="account",
                name=account_names.get(account_id, f"账号 #{account_id}"),
                related_id=str(account_id),
                action=action,
            )
    sort_key = lambda item: (item.last_used_at or "", item.action_count)
    return (
        sorted(target_metrics.values(), key=sort_key, reverse=True)[:100],
        sorted(account_metrics.values(), key=sort_key, reverse=True)[:100],
        sorted(keyword_metrics.values(), key=sort_key, reverse=True)[:100],
    )


def _rule_trend_metrics(session: Session, tenant_id: int, days: int = 14) -> list[RuleTrendMetricOut]:
    end_date = _now().date()
    start_date = end_date - timedelta(days=days - 1)
    buckets = {
        (start_date + timedelta(days=offset)).isoformat(): RuleTrendMetricOut(date=(start_date + timedelta(days=offset)).isoformat())
        for offset in range(days)
    }
    actions = list(
        session.scalars(
            select(Action)
            .where(
                Action.tenant_id == tenant_id,
                Action.task_type == "group_relay",
                Action.action_type == "send_message",
                Action.created_at >= datetime.combine(start_date, datetime.min.time()),
            )
            .order_by(Action.created_at.desc())
            .limit(5000)
        )
    )
    for action in actions:
        occurred_at = action.executed_at or action.scheduled_at or action.created_at
        if not occurred_at:
            continue
        date_key = occurred_at.date().isoformat()
        bucket = buckets.get(date_key)
        if not bucket:
            continue
        bucket.action_count += 1
        if action.status == "success":
            bucket.success_count += 1
        elif action.status == "failed":
            bucket.failed_count += 1
        elif action.status == "skipped":
            bucket.skipped_count += 1
        else:
            bucket.pending_count += 1
    return list(buckets.values())


def _rule_conversion_metrics(session: Session, tenant_id: int, days: int = 7) -> list[RuleConversionMetricOut]:
    today = _now().date()
    current_start = today - timedelta(days=days - 1)
    previous_start = current_start - timedelta(days=days)
    versions = {
        version.id: version
        for version in session.scalars(select(RuleSetVersion).where(RuleSetVersion.tenant_id == tenant_id))
    }
    rule_sets = {
        rule_set.id: rule_set
        for rule_set in session.scalars(select(RuleSet).where(RuleSet.tenant_id == tenant_id))
    }
    metrics: dict[str, RuleConversionMetricOut] = {}
    actions = list(
        session.scalars(
            select(Action)
            .where(
                Action.tenant_id == tenant_id,
                Action.task_type == "group_relay",
                Action.action_type == "send_message",
                Action.created_at >= datetime.combine(previous_start, datetime.min.time()),
            )
            .order_by(Action.created_at.desc())
            .limit(5000)
        )
    )
    for action in actions:
        occurred_at = action.executed_at or action.scheduled_at or action.created_at
        if not occurred_at:
            continue
        action_date = occurred_at.date()
        period = "current" if action_date >= current_start else "previous" if previous_start <= action_date < current_start else ""
        if not period:
            continue
        payload = action.payload if isinstance(action.payload, dict) else {}
        version_id = _as_int(payload.get("resolved_rule_set_version_id")) or _as_int(payload.get("rule_set_version_id"))
        rule_set_id = _as_int(payload.get("rule_set_id"))
        if not version_id and not rule_set_id:
            continue
        version = versions.get(version_id or 0)
        if version and not rule_set_id:
            rule_set_id = version.rule_set_id
        rule_set = rule_sets.get(rule_set_id or 0)
        key = f"rule-version:{version_id}" if version_id else f"rule-set:{rule_set_id}"
        metric = metrics.get(key)
        if metric is None:
            metric = RuleConversionMetricOut(
                key=key,
                rule_set_id=rule_set_id,
                rule_set_version_id=version_id,
                rule_set_name=rule_set.name if rule_set else "",
                version=version.version if version else None,
            )
            metrics[key] = metric
        if period == "current":
            metric.current_action_count += 1
            if action.status == "success":
                metric.current_success_count += 1
        else:
            metric.previous_action_count += 1
            if action.status == "success":
                metric.previous_success_count += 1
    for metric in metrics.values():
        metric.current_success_rate = _rate(metric.current_success_count, metric.current_action_count)
        metric.previous_success_rate = _rate(metric.previous_success_count, metric.previous_action_count)
        metric.success_rate_delta = metric.current_success_rate - metric.previous_success_rate
    return sorted(metrics.values(), key=lambda item: (item.current_action_count, item.previous_action_count), reverse=True)[:100]


def _rule_cross_metrics(session: Session, tenant_id: int) -> list[RuleCrossMetricOut]:
    versions = {
        version.id: version
        for version in session.scalars(select(RuleSetVersion).where(RuleSetVersion.tenant_id == tenant_id))
    }
    rule_sets = {
        rule_set.id: rule_set
        for rule_set in session.scalars(select(RuleSet).where(RuleSet.tenant_id == tenant_id))
    }
    target_names = {
        group.id: group.title
        for group in session.scalars(select(TgGroup).where(TgGroup.tenant_id == tenant_id))
    }
    account_names = {
        account.id: account.display_name
        for account in session.scalars(select(TgAccount).where(TgAccount.tenant_id == tenant_id))
    }
    metrics: dict[str, RuleCrossMetricOut] = {}
    actions = list(
        session.scalars(
            select(Action)
            .where(
                Action.tenant_id == tenant_id,
                Action.task_type == "group_relay",
                Action.action_type == "send_message",
            )
            .order_by(Action.created_at.desc())
            .limit(5000)
        )
    )
    for action in actions:
        payload = action.payload if isinstance(action.payload, dict) else {}
        version_id = _as_int(payload.get("resolved_rule_set_version_id")) or _as_int(payload.get("rule_set_version_id"))
        rule_set_id = _as_int(payload.get("rule_set_id"))
        target_id = _as_int(payload.get("group_id"))
        account_id = int(action.account_id or 0)
        if not (version_id or rule_set_id) or not target_id or not account_id:
            continue
        version = versions.get(version_id or 0)
        if version and not rule_set_id:
            rule_set_id = version.rule_set_id
        rule_set = rule_sets.get(rule_set_id or 0)
        key = f"cross:{version_id or rule_set_id}:{target_id}:{account_id}"
        metric = metrics.get(key)
        if metric is None:
            metric = RuleCrossMetricOut(
                key=key,
                rule_set_id=rule_set_id,
                rule_set_version_id=version_id,
                rule_set_name=rule_set.name if rule_set else "",
                version=version.version if version else None,
                target_group_id=target_id,
                target_name=target_names.get(target_id, f"目标群 #{target_id}"),
                account_id=account_id,
                account_name=account_names.get(account_id, f"账号 #{account_id}"),
            )
            metrics[key] = metric
        metric.action_count += 1
        if action.status == "success":
            metric.success_count += 1
        elif action.status == "failed":
            metric.failed_count += 1
        elif action.status == "skipped":
            metric.skipped_count += 1
        else:
            metric.pending_count += 1
        occurred_at = action.executed_at or action.scheduled_at or action.created_at
        if occurred_at and (metric.last_used_at is None or (_iso(occurred_at) or "") > metric.last_used_at):
            metric.last_used_at = _iso(occurred_at)
    for metric in metrics.values():
        metric.success_rate = _rate(metric.success_count, metric.action_count)
    return sorted(metrics.values(), key=lambda item: (item.action_count, item.success_rate, item.last_used_at or ""), reverse=True)[:100]


def _touch_rule_dimension_metric(
    metrics: dict[str, RuleDimensionMetricOut],
    *,
    key: str,
    dimension: Literal["target", "account", "keyword"],
    name: str,
    related_id: str,
    action: Action,
) -> None:
    metric = metrics.get(key)
    if metric is None:
        metric = RuleDimensionMetricOut(key=key, dimension=dimension, name=name, related_id=related_id)
        metrics[key] = metric
    metric.action_count += 1
    if action.status == "success":
        metric.success_count += 1
    elif action.status == "failed":
        metric.failed_count += 1
    elif action.status == "skipped":
        metric.skipped_count += 1
    else:
        metric.pending_count += 1
    occurred_at = action.executed_at or action.scheduled_at or action.created_at
    if occurred_at and (metric.last_used_at is None or (_iso(occurred_at) or "") > metric.last_used_at):
        metric.last_used_at = _iso(occurred_at)


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


def _get_rule_set_version(session: Session, tenant_id: int, rule_set_id: int, version_id: int) -> RuleSetVersion:
    version = session.get(RuleSetVersion, version_id)
    if not version or version.tenant_id != tenant_id or version.rule_set_id != rule_set_id:
        raise ValueError("规则集版本不存在")
    return version


def _version_payload_from_row(version: RuleSetVersion, *, version_note: str) -> RuleSetVersionCreate:
    return RuleSetVersionCreate(
        version_note=version_note,
        filters=dict(version.filters or {}),
        output_checks=dict(version.output_checks or {}),
        transforms=dict(version.transforms or {}),
        routing=dict(version.routing or {}),
        account_strategy=dict(version.account_strategy or {}),
        rate_limits=dict(version.rate_limits or {}),
        retry_policy=dict(version.retry_policy or {}),
    )


def _new_rule_set_version(session: Session, tenant_id: int, rule_set_id: int, version: int, payload: RuleSetVersionCreate, actor: str) -> RuleSetVersion:
    row = RuleSetVersion(
        tenant_id=tenant_id,
        rule_set_id=rule_set_id,
        version=version,
        status="draft",
        version_note=payload.version_note,
        filters=payload.filters,
        output_checks=payload.output_checks,
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
        task_types=rule_set.task_types or [],
        default_policy=rule_set.default_policy or {},
        active_version_id=rule_set.active_version_id,
        versions=[
            {
                "id": version.id,
                "tenant_id": version.tenant_id,
                "rule_set_id": version.rule_set_id,
                "version": version.version,
                "status": version.status,
                "version_note": version.version_note,
                "filters": version.filters or {},
                "output_checks": version.output_checks or {},
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
    "copy_rule_set_version",
    "create_rule_set",
    "create_rule_set_version",
    "listener_summary",
    "list_rule_set_bound_tasks",
    "list_rule_sets",
    "operation_metrics_summary",
    "publish_rule_set_version",
    "relay_attribution_csv",
    "relay_attribution_report",
    "rollback_rule_set_version",
    "rule_center_summary",
    "test_rules",
    "update_rule_set_config",
]
