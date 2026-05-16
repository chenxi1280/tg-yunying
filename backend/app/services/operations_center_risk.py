from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, ContentKeywordRule, MessageFingerprint, OperationTarget, RuntimeMetricSnapshot, SchedulingSetting, Task, TgAccount, TgGroup, WorkerHeartbeat
from app.schemas.operations_center import MetricBucketOut, OperationMetricDetailOut
from app.services._common import _as_utc, _now
from app.services.operations_center_defaults import ACTIVE_TASK_STATUSES
from app.services.operations_center_utils import iso as _iso


def _metric(key: str, label: str, value: int | float | str, detail: str = "", status: str = "") -> MetricBucketOut:
    return MetricBucketOut(key=key, label=label, value=value, detail=detail, status=status)


def _metric_detail(key: str, title: str, category: str, status: str, detail: str = "", related_id: str = "", occurred_at: datetime | str | None = None) -> OperationMetricDetailOut:
    return OperationMetricDetailOut(key=key, title=title, category=category, status=status, detail=detail, related_id=related_id, occurred_at=_iso(occurred_at))


def _count(session: Session, model, *conditions) -> int:
    return int(session.scalar(select(func.count(model.id)).where(*conditions)) or 0)


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



def risk_control_metrics(session: Session, tenant_id: int) -> list[MetricBucketOut]:
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
    runtime_metrics = _latest_runtime_metrics(session)
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
        _metric(
            "runtime.pending_actions",
            "执行积压",
            runtime_metrics.get("actions.pending.count", 0),
            f"claiming {runtime_metrics.get('actions.claiming.count', 0)} / executing {runtime_metrics.get('actions.executing.count', 0)} / 最老等待 {runtime_metrics.get('actions.oldest_pending_age_seconds', 0)}s",
        ),
        _metric("runtime.unknown_after_send", "结果未知", runtime_metrics.get("actions.unknown_after_send.count", 0), "已进入 TG 调用边界但本地结果未知的执行项"),
    ]


def _latest_runtime_metrics(session: Session) -> dict[str, int]:
    latest = session.scalar(select(func.max(RuntimeMetricSnapshot.captured_at)))
    if not latest:
        return {}
    rows = session.execute(
        select(RuntimeMetricSnapshot.metric_name, RuntimeMetricSnapshot.metric_value).where(
            RuntimeMetricSnapshot.captured_at == latest,
            RuntimeMetricSnapshot.dimension_type == "global",
            RuntimeMetricSnapshot.dimension_id == "all",
        )
    ).all()
    return {name: int(value or 0) for name, value in rows}


def risk_control_details(session: Session, tenant_id: int) -> list[OperationMetricDetailOut]:
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



__all__ = ["risk_control_metrics", "risk_control_details"]
