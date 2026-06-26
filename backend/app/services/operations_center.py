from __future__ import annotations

import csv
from io import StringIO
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, AiUsageLedger, ContentKeywordRule, GroupArchive, OperationTarget, RuleSet, RuleSetVersion, Task, TgAccount, TgGroup, TgGroupAccount
from app.schemas.operations_center import (
    MetricBucketOut,
    OperationMetricDetailOut,
    OperationMetricsOut,
    RelayAttributionReportOut,
    RelayMaterialAttributionOut,
    RuleCenterSummaryOut,
    RuleTestCandidateOut,
    RuleTestHitOut,
    RuleTestOut,
    RuleTestRouteOut,
    RuleTestSimulationStepOut,
)
from app.services._common import _now, audit
from app.services.operation_login_drop_rates import account_pool_login_drop_rates
from app.services.rule_engine import apply_output_policy, evaluate_input_filter
from app.services.material_rules import select_material_for_policy
from app.services.task_center.executors.group_relay import apply_transform_rules, relay_filter_expression_reason, resolve_relay_target_ids
from app.services.task_center.fingerprints import content_fingerprint


from app.services.operations_center_learning import listener_learning_profile, listener_learning_samples, refresh_listener_learning
from app.services.operations_center_listener import (
    list_listener_errors,
    list_listener_events,
    listener_summary,
    reset_listener_watermark,
    switch_listener_account,
)
from app.services.operations_center_defaults import (
    ACTIVE_TASK_STATUSES,
    SYSTEM_RULES,
)
from app.services.operations_center_utils import as_int as _as_int, as_int_list as _as_int_list, iso as _iso
from app.services.operations_center_risk import _is_stale_heartbeat, risk_control_details, risk_control_metrics
from app.services.operations_center_rule_metrics import (
    _keyword_rule_summary,
    _relay_task_rule_summary,
    _rule_conflicts,
    _rule_conversion_metrics,
    _rule_cross_metrics,
    _rule_dimension_metrics,
    _rule_execution_metrics,
    _rule_set_summary,
    _rule_trend_metrics,
)
from app.services.operations_center_rule_sets import (
    copy_rule_set_version,
    create_rule_set,
    create_rule_set_version,
    list_rule_set_bound_tasks,
    list_rule_sets,
    publish_rule_set_version,
    rollback_rule_set_version,
    update_rule_set_config,
)

UNRESOLVED_FAILURE_STATUSES = {"failed", "retryable_failed", "unknown_after_send"}


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
        _touch_relay_attribution_metric(metrics, action=action, task=task)
    report_rows = [_relay_attribution_row(fingerprint, metric) for fingerprint, metric in metrics.items()]
    report_rows.sort(key=lambda item: (item.action_count, item.last_used_at or ""), reverse=True)
    return RelayAttributionReportOut(
        total_materials=len(report_rows),
        total_source_events=sum(item.source_event_count for item in report_rows),
        total_actions=sum(item.action_count for item in report_rows),
        rows=report_rows,
    )


def _touch_relay_attribution_metric(metrics: dict[str, dict[str, Any]], *, action: Action, task: Task) -> None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    batch_id = str(payload.get("relay_batch_id") or "")
    if not batch_id:
        return
    original_text = str(payload.get("original_text") or "")
    transformed_text = str(payload.get("message_text") or "")
    material_text = original_text or transformed_text
    fingerprint = str(payload.get("material_fingerprint") or "") or (content_fingerprint(material_text) if material_text else "")
    metric = metrics.setdefault(fingerprint or f"batch:{batch_id}", _empty_relay_attribution_metric(material_text))
    metric["sample_text"] = metric["sample_text"] or material_text[:160]
    metric["task_ids"].add(task.id)
    _touch_relay_attribution_sets(metric, action=action, payload=payload)
    metric["action_count"] += 1
    metric["retry_count"] += int(action.retry_count or 0)
    _add_relay_status_count(metric, action.status)
    occurred_at = action.executed_at or action.scheduled_at or action.created_at
    if occurred_at and (metric["last_used_at"] is None or occurred_at > metric["last_used_at"]):
        metric["last_used_at"] = occurred_at


def _empty_relay_attribution_metric(material_text: str) -> dict[str, Any]:
    return {
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
    }


def _touch_relay_attribution_sets(metric: dict[str, Any], *, action: Action, payload: dict[str, Any]) -> None:
    source_event = str(payload.get("source_event_key") or payload.get("relay_event_id") or "")
    if source_event:
        metric["source_events"].add(source_event)
    target_id = payload.get("operation_target_id") or payload.get("target_group_id")
    if target_id:
        metric["targets"].add(str(target_id))
    if action.account_id:
        metric["accounts"].add(str(action.account_id))


def _add_relay_status_count(metric: dict[str, Any], status: str) -> None:
    if status == "success":
        metric["success_count"] += 1
    elif status in UNRESOLVED_FAILURE_STATUSES:
        metric["failed_count"] += 1
    elif status == "skipped":
        metric["skipped_count"] += 1
    else:
        metric["pending_count"] += 1


def _relay_attribution_row(fingerprint: str, metric: dict[str, Any]) -> RelayMaterialAttributionOut:
    action_count = int(metric["action_count"] or 0)
    success_count = int(metric["success_count"] or 0)
    return RelayMaterialAttributionOut(
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
    failed_actions = _count(session, Action, Action.tenant_id == tenant_id, Action.status.in_(UNRESOLVED_FAILURE_STATUSES))
    skipped_actions = _count(session, Action, Action.tenant_id == tenant_id, Action.status == "skipped")

    channel_actions = _count(session, Action, Action.tenant_id == tenant_id, Action.action_type.in_(["view_message", "like_message", "post_comment"]))
    channel_success = _count(session, Action, Action.tenant_id == tenant_id, Action.action_type.in_(["view_message", "like_message", "post_comment"]), Action.status == "success")
    channel_failed = _count(session, Action, Action.tenant_id == tenant_id, Action.action_type.in_(["view_message", "like_message", "post_comment"]), Action.status.in_(UNRESOLVED_FAILURE_STATUSES))

    ai_tasks = _count(session, Task, Task.tenant_id == tenant_id, Task.type == "group_ai_chat", Task.deleted_at.is_(None))
    ai_turns = _count(session, Action, Action.tenant_id == tenant_id, Action.task_type == "group_ai_chat", Action.action_type == "send_message")
    ai_sent_turns = _count(session, Action, Action.tenant_id == tenant_id, Action.task_type == "group_ai_chat", Action.status == "success")

    relay_tasks = _count(session, Task, Task.tenant_id == tenant_id, Task.type == "group_relay", Task.deleted_at.is_(None))
    relay_items = _count(session, Action, Action.tenant_id == tenant_id, Action.task_type == "group_relay", Action.action_type == "send_message")
    relay_sent = _count(session, Action, Action.tenant_id == tenant_id, Action.task_type == "group_relay", Action.status == "success")
    relay_failed = _count(session, Action, Action.tenant_id == tenant_id, Action.task_type == "group_relay", Action.status.in_(UNRESOLVED_FAILURE_STATUSES))

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
        account_pool_login_drop_rates=account_pool_login_drop_rates(session, tenant_id),
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
            _metric("failures.actions", "失败/结果未知执行项", failed_actions, "需要重试、换号、人工确认或排查"),
            _metric("failures.skipped", "跳过执行项", skipped_actions, "自动校验、上下文过期或规则过滤跳过"),
            _metric("failures.rate", "失败率", f"{_rate(failed_actions, total_actions)}%", "按全部执行项计算"),
        ],
        risk_control=risk_control_metrics(session, tenant_id),
        account_details=_account_metric_details(session, tenant_id),
        target_details=_target_metric_details(session, tenant_id),
        task_details=_task_metric_details(session, tenant_id),
        failure_details=_failure_metric_details(session, tenant_id),
        risk_details=risk_control_details(session, tenant_id),
    )


def test_rules(
    session: Session,
    tenant_id: int,
    text: str,
    test_type: str = "group_relay",
    test_mode: str = "rules_only",
    simulation_scenario: str = "",
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
    simulation_steps = _rule_test_media_simulation(simulation_scenario)
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
        material_result = select_material_for_policy(
            session,
            tenant_id,
            (version.routing or {}).get("material_policy") or {},
            context_key=f"rule-test:{version.id}:{transformed_text}",
        )
        block_reasons: list[str] = []
        if not filter_passed:
            block_reasons.append(filter_reason or "未通过规则集过滤")
        return RuleTestOut(
            result="规则版本预览：通过过滤，已计算转换和路由" if filter_passed else "规则版本预览：未通过过滤",
            test_mode=test_mode,
            is_test_data=True,
            simulation_scenario=simulation_scenario,
            simulation_steps=simulation_steps,
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
            material_candidate_count=material_result.candidate_count,
            material_selected_id=material_result.selected.id if material_result.selected else None,
            material_action=material_result.action,
            material_failure_reason=material_result.failure_reason,
            target_summary=target_summary,
            target_routes=routes,
            account_strategy=account_summary,
            rate_limit_summary=rate_summary,
        )
    return RuleTestOut(
        result="未命中规则条件",
        test_mode=test_mode,
        is_test_data=True,
        simulation_scenario=simulation_scenario,
        simulation_steps=simulation_steps,
        hits=[],
        should_block=False,
        block_reason="",
        transformed_text=text,
        target_summary="规则测试只验证内容规则；实际目标由任务绑定的路由配置决定",
        account_strategy="规则测试不占用账号；执行时按任务账号池、冷却和目标粘性策略选择",
        rate_limit_summary="测试不触发限流；执行时按账号冷却、小时/日上限与失败重试策略校验",
    )


def _rule_test_media_simulation(scenario: str) -> list[RuleTestSimulationStepOut]:
    scenario = (scenario or "").strip()
    if not scenario:
        return []
    scenarios: dict[str, list[tuple[str, str, str, str]]] = {
        "pending_cache": [
            ("源媒体入队", "pending_cache", "等待本轮超时或按规则降级", "只创建同一个 source_media_asset_id，不在发送现场上传"),
            ("执行项检查", "waiting_material_cache", "任务保持等待或跳过媒体", "运行中任务优先，暂停任务不唤醒发送"),
        ],
        "timeout_then_cached": [
            ("等待超时", "material_cache_wait_timeout", "丢弃媒体并降级文本或跳过", "超时后执行项已经完成本轮处置"),
            ("缓存完成事件", "late_event", "拒绝补发", "缓存完成晚于本轮超时，只记录迟到事件"),
        ],
        "late_cache_event": [
            ("缓存事件到达", "stale_event", "拒绝唤醒", "事件版本早于执行项固化的素材版本或缓存版本"),
            ("执行项状态", "unchanged", "保持已完成处置结果", "旧事件只进入审计，不改写发送结果"),
        ],
        "album_one_failed": [
            ("相册分段 1", "ready", "按原顺序发送", "media_group_index=1"),
            ("相册分段 2", "album_segment_failed", "剔除失败图", "允许去掉失败图继续发"),
            ("相册分段 3", "ready", "保持原顺序继续发送", "后续图片不能因为中间失败而乱序"),
        ],
        "queue_overflow": [
            ("等待队列检查", "material_cache_wait_queue_full", "放弃受影响素材缓存", "队列爆满时不创建人工缓存动作"),
            ("清理等待项", "abandoned", "记录失败原因并按规则降级或跳过", "不可恢复素材暴增和兜底扫描频繁唤醒也走同类保护"),
        ],
    }
    return [
        RuleTestSimulationStepOut(step=step, status=status, action=action, reason=reason)
        for step, status, action, reason in scenarios.get(scenario, [])
    ]


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
        .where(Action.tenant_id == tenant_id, Action.status.in_([*UNRESOLVED_FAILURE_STATUSES, "skipped"]))
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


__all__ = [
    "copy_rule_set_version",
    "create_rule_set",
    "create_rule_set_version",
    "listener_learning_profile",
    "listener_learning_samples",
    "list_listener_errors",
    "list_listener_events",
    "listener_summary",
    "list_rule_set_bound_tasks",
    "list_rule_sets",
    "operation_metrics_summary",
    "publish_rule_set_version",
    "refresh_listener_learning",
    "relay_attribution_csv",
    "relay_attribution_report",
    "reset_listener_watermark",
    "rollback_rule_set_version",
    "rule_center_summary",
    "test_rules",
    "update_rule_set_config",
]
