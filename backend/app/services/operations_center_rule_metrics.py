from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ContentKeywordRule, RuleSet, RuleSetVersion, Task, TgAccount, TgGroup
from app.schemas.operations_center import (
    RuleConflictOut,
    RuleConversionMetricOut,
    RuleCrossMetricOut,
    RuleDimensionMetricOut,
    RuleExecutionMetricOut,
    RuleSetOut,
    RuleSummaryOut,
    RuleTrendMetricOut,
)
from app.services._common import _now
from app.services.operations_center_utils import as_int as _as_int, iso as _iso
from app.services.rule_engine import task_type_labels


def _rate(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round(float(numerator) / float(denominator) * 100, 1)


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

