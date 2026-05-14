from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import socket
import time
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    AccountProxy,
    AccountProxyBinding,
    AccountStatus,
    Action,
    FailureType,
    ManualOperationRecord,
    MessageTask,
    MessageTaskAttempt,
    OperationTaskAttempt,
    OperationTarget,
    ProxyAlert,
    ProxyHealthCheck,
    SchedulingSetting,
    Task,
    TaskStatus,
    TgAccount,
)
from app.schemas.risk_control import AccountProxyCreate, AccountProxyUpdate, ProxyBatchBindingRequest, ProxyBindingRequest, RiskControlGlobalPolicyUpdate, RiskPreflightRequest
from app.security import encrypt_secret
from app.services._common import _now, audit
from app.services.account_capacity import account_capacity_decision
from app.services.ai_config import get_scheduling_setting
from app.services.content_filters import tenant_keyword_rules


ACTION_OCCUPIED_STATUSES = {"pending", "executing", "success"}
MESSAGE_TASK_OCCUPIED_STATUSES = {TaskStatus.QUEUED.value, TaskStatus.SENDING.value, TaskStatus.SENT.value}
FAILED_STATUSES = {"failed", "skipped", TaskStatus.FAILED.value, "失败", "已跳过"}
BLOCKED_ACCOUNT_STATUSES = {
    AccountStatus.PENDING_LOGIN.value,
    AccountStatus.WAITING_CODE.value,
    AccountStatus.WAITING_QR.value,
    AccountStatus.WAITING_2FA.value,
    AccountStatus.NEED_RELOGIN.value,
    AccountStatus.SESSION_EXPIRED.value,
    AccountStatus.LIMITED.value,
    AccountStatus.SUSPECTED_BANNED.value,
    AccountStatus.BANNED.value,
    AccountStatus.DISABLED.value,
    AccountStatus.ERROR.value,
}
LOGIN_DISPOSITION_STATUSES = {
    AccountStatus.PENDING_LOGIN.value,
    AccountStatus.WAITING_CODE.value,
    AccountStatus.WAITING_QR.value,
    AccountStatus.WAITING_2FA.value,
}


def list_account_proxies(session: Session, tenant_id: int) -> list[dict[str, Any]]:
    proxies = list(session.scalars(select(AccountProxy).where(AccountProxy.tenant_id == tenant_id).order_by(AccountProxy.id.asc())))
    bound_counts = _proxy_bound_counts(session, tenant_id, [proxy.id for proxy in proxies])
    return [_proxy_payload(proxy, bound_counts.get(proxy.id, 0)) for proxy in proxies]


def update_global_policy(session: Session, tenant_id: int, payload: RiskControlGlobalPolicyUpdate, actor: str) -> dict[str, Any]:
    setting = get_scheduling_setting(session, tenant_id)
    data = payload.model_dump(exclude_unset=True)
    for field in [
        "jitter_min_seconds",
        "jitter_max_seconds",
        "batch_interval_seconds",
        "respect_send_window",
        "quiet_hours_enabled",
        "quiet_start",
        "quiet_end",
        "quiet_timezone",
        "default_max_retries",
        "default_retry_delay_seconds",
        "default_retry_backoff",
        "default_on_account_banned",
        "default_on_api_rate_limit",
        "default_on_content_rejected",
        "default_account_hour_limit",
        "default_account_day_limit",
        "default_account_cooldown_seconds",
    ]:
        if field in data and data[field] is not None:
            setattr(setting, field, data[field])
    if setting.jitter_max_seconds < setting.jitter_min_seconds:
        setting.jitter_max_seconds = setting.jitter_min_seconds
    setting.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="更新风控全局策略", target_type="risk_global_policy", target_id=str(setting.id))
    session.commit()
    session.refresh(setting)
    return _global_policy(setting)


def create_account_proxy(session: Session, tenant_id: int, payload: AccountProxyCreate, actor: str) -> dict[str, Any]:
    trace_id = _trace_id()
    proxy = AccountProxy(
        tenant_id=tenant_id,
        name=payload.name.strip(),
        protocol=payload.protocol,
        host=payload.host.strip() or "127.0.0.1",
        port=payload.port,
        username=payload.username.strip(),
        password_ciphertext=encrypt_secret(payload.password) if payload.password else "",
        check_interval_seconds=payload.check_interval_seconds,
        timeout_ms=payload.timeout_ms,
        max_bound_accounts=payload.max_bound_accounts,
        max_concurrent_sessions=payload.max_concurrent_sessions,
        notes=payload.notes.strip(),
    )
    session.add(proxy)
    session.flush()
    audit(session, tenant_id=tenant_id, actor=actor, action="新增本地代理资源", target_type="account_proxy", target_id=str(proxy.id), detail=f"trace_id={trace_id}")
    session.commit()
    session.refresh(proxy)
    return _proxy_payload(proxy, 0, trace_id=trace_id)


def update_account_proxy(session: Session, tenant_id: int, proxy_id: int, payload: AccountProxyUpdate, actor: str) -> dict[str, Any]:
    proxy = _require_proxy(session, tenant_id, proxy_id)
    trace_id = _trace_id()
    changed: list[str] = []
    data = payload.model_dump(exclude_unset=True)
    for field in ["name", "protocol", "host", "port", "username", "check_interval_seconds", "timeout_ms", "max_bound_accounts", "max_concurrent_sessions", "notes"]:
        if field in data and data[field] is not None and getattr(proxy, field) != data[field]:
            setattr(proxy, field, data[field])
            changed.append(field)
    if payload.password_reset is not None:
        proxy.password_ciphertext = encrypt_secret(payload.password_reset) if payload.password_reset else ""
        changed.append("password_reset")
    proxy.updated_at = _now()
    affected = _proxy_bound_counts(session, tenant_id, [proxy.id]).get(proxy.id, 0)
    audit(session, tenant_id=tenant_id, actor=actor, action="编辑本地代理资源", target_type="account_proxy", target_id=str(proxy.id), detail=f"trace_id={trace_id}; changed={','.join(changed)}; reason={payload.change_reason}")
    session.commit()
    session.refresh(proxy)
    result = _proxy_payload(proxy, affected, trace_id=trace_id)
    result.update({"changed_fields": changed, "affected_account_count": affected})
    return result


def check_account_proxy(session: Session, tenant_id: int, proxy_id: int, *, check_type: str, reason: str, actor: str) -> dict[str, Any]:
    proxy = _require_proxy(session, tenant_id, proxy_id)
    trace_id = _trace_id()
    status, latency_ms, error_code, error_detail = _probe_proxy(proxy)
    proxy.status = "healthy" if status == "healthy" else "unhealthy"
    proxy.alert_status = "normal" if status == "healthy" else "alerting"
    proxy.last_check_at = _now()
    proxy.last_error = error_detail
    if status == "healthy":
        proxy.disabled_reason = ""
    proxy.updated_at = _now()
    check = ProxyHealthCheck(
        tenant_id=tenant_id,
        proxy_id=proxy.id,
        check_type="port_connect" if check_type == "quick" else check_type,
        status=status,
        latency_ms=latency_ms,
        error_code=error_code,
        error_detail=error_detail,
        checked_by=actor,
        trace_id=trace_id,
    )
    session.add(check)
    related_alert_id = None
    if status != "healthy":
        alert = _upsert_proxy_alert(
            session,
            proxy,
            severity="critical",
            alert_type="不可达",
            reason_code=error_code,
            suggested_action="检查服务器代理软件或切换账号绑定代理",
        )
        related_alert_id = alert.id
    else:
        _recover_proxy_alerts(session, proxy)
    audit(session, tenant_id=tenant_id, actor=actor, action="检查本地代理", target_type="account_proxy", target_id=str(proxy.id), detail=f"trace_id={trace_id}; status={status}; reason={reason}")
    session.commit()
    session.refresh(check)
    return {
        "id": check.id,
        "proxy_id": proxy.id,
        "check_type": check.check_type,
        "status": check.status,
        "latency_ms": check.latency_ms,
        "error_code": check.error_code,
        "error_detail": check.error_detail,
        "checked_by": check.checked_by,
        "checked_at": check.checked_at,
        "trace_id": trace_id,
        "related_alert_id": related_alert_id,
    }


def disable_account_proxy(session: Session, tenant_id: int, proxy_id: int, disabled_reason: str, actor: str) -> dict[str, Any]:
    proxy = _require_proxy(session, tenant_id, proxy_id)
    trace_id = _trace_id()
    proxy.status = "disabled"
    proxy.alert_status = "disabled"
    proxy.disabled_reason = disabled_reason
    proxy.updated_at = _now()
    _upsert_proxy_alert(session, proxy, severity="critical", alert_type="禁用", reason_code="proxy_disabled", suggested_action="切换账号绑定代理或恢复本地代理")
    audit(session, tenant_id=tenant_id, actor=actor, action="禁用本地代理", target_type="account_proxy", target_id=str(proxy.id), detail=f"trace_id={trace_id}; reason={disabled_reason}")
    session.commit()
    session.refresh(proxy)
    return _proxy_payload(proxy, _proxy_bound_counts(session, tenant_id, [proxy.id]).get(proxy.id, 0), trace_id=trace_id)


def bind_account_proxy(session: Session, tenant_id: int, account_id: int, payload: ProxyBindingRequest, actor: str) -> dict[str, Any]:
    account = _require_account(session, tenant_id, account_id)
    proxy = _require_proxy(session, tenant_id, payload.proxy_id) if payload.proxy_id is not None else None
    trace_id = _trace_id()
    warnings: list[str] = []
    if proxy and payload.run_precheck:
        warnings = _proxy_binding_warnings(session, tenant_id, proxy, exclude_account_id=account.id)
        if warnings:
            raise ValueError("代理预检查未通过：" + "；".join(warnings))
    old_proxy_id = account.proxy_id
    account.proxy_id = proxy.id if proxy else None
    session.add(AccountProxyBinding(tenant_id=tenant_id, account_id=account.id, proxy_id=account.proxy_id, change_reason=payload.change_reason, bound_by=actor))
    audit(session, tenant_id=tenant_id, actor=actor, action="绑定账号本地代理", target_type="tg_account", target_id=str(account.id), detail=f"trace_id={trace_id}; old={old_proxy_id}; new={account.proxy_id}; reason={payload.change_reason}")
    session.commit()
    return {
        "account_id": account.id,
        "old_proxy_id": old_proxy_id,
        "new_proxy_id": account.proxy_id,
        "proxy_status": proxy.status if proxy else "",
        "proxy_alert_status": proxy.alert_status if proxy else "",
        "affected_pending_action_count": _action_count_for_account(session, tenant_id, account.id, "pending"),
        "affected_running_action_count": _action_count_for_account(session, tenant_id, account.id, "executing"),
        "warnings": warnings,
        "trace_id": trace_id,
        "audit_id": "",
    }


def bind_accounts_proxy_batch(session: Session, tenant_id: int, payload: ProxyBatchBindingRequest, actor: str) -> dict[str, Any]:
    trace_id = _trace_id()
    skipped: list[dict[str, Any]] = []
    affected: list[int] = []
    pending = 0
    executing = 0
    for account_id in payload.account_ids:
        try:
            proxy_id = payload.manual_bindings.get(str(account_id), payload.proxy_id) if payload.assignment_mode == "manual_map" else payload.proxy_id
            result = bind_account_proxy(session, tenant_id, account_id, ProxyBindingRequest(proxy_id=proxy_id, change_reason=payload.change_reason, run_precheck=payload.run_precheck), actor)
            affected.append(account_id)
            pending += int(result["affected_pending_action_count"])
            executing += int(result["affected_running_action_count"])
        except ValueError as exc:
            skipped.append({"account_id": account_id, "reason": str(exc)})
    audit(session, tenant_id=tenant_id, actor=actor, action="批量绑定账号本地代理", target_type="account_proxy", target_id=str(payload.proxy_id or ""), detail=f"trace_id={trace_id}; success={len(affected)}; failed={len(skipped)}; reason={payload.change_reason}")
    session.commit()
    return {
        "success_count": len(affected),
        "failed_count": len(skipped),
        "skipped_accounts": skipped,
        "affected_account_ids": affected,
        "affected_pending_action_count": pending,
        "affected_running_action_count": executing,
        "warnings": [],
        "trace_id": trace_id,
        "audit_id": "",
    }


def list_proxy_alerts(session: Session, tenant_id: int) -> list[dict[str, Any]]:
    alerts = list(
        session.scalars(
            select(ProxyAlert)
            .where(ProxyAlert.tenant_id == tenant_id)
            .order_by(ProxyAlert.status.asc(), ProxyAlert.last_seen_at.desc())
            .limit(200)
        )
    )
    return [_proxy_alert_payload(alert) for alert in alerts]


def update_proxy_alert_status(session: Session, tenant_id: int, alert_id: int, status: str, actor: str, *, reason: str = "", ignored_until: datetime | None = None) -> dict[str, Any]:
    alert = session.get(ProxyAlert, alert_id)
    if not alert or alert.tenant_id != tenant_id:
        raise ValueError("proxy alert not found")
    now = _now()
    if status == "recovered":
        proxy = session.get(AccountProxy, alert.proxy_id)
        if not proxy or proxy.tenant_id != tenant_id:
            raise ValueError("proxy alert not found")
        if proxy.status != "healthy" or proxy.alert_status != "normal":
            raise ValueError("请先完成代理健康检查，确认恢复后再关闭告警")

    alert.status = status
    if status == "acknowledged":
        alert.acknowledged_by = actor
        alert.acknowledged_at = now
    if status == "ignored":
        alert.ignored_until = ignored_until
    if status == "recovered":
        alert.recovered_at = now
    audit(session, tenant_id=tenant_id, actor=actor, action="处理代理告警", target_type="proxy_alert", target_id=str(alert.id), detail=f"status={status}; reason={reason}")
    session.commit()
    session.refresh(alert)
    return _proxy_alert_payload(alert)


def risk_preflight(session: Session, tenant_id: int, payload: RiskPreflightRequest) -> dict[str, Any]:
    trace_id = _trace_id()
    scheduled_at = payload.scheduled_at or _now()
    account_query = select(TgAccount).options(selectinload(TgAccount.pool), selectinload(TgAccount.proxy)).where(TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None))
    if payload.account_ids:
        account_query = account_query.where(TgAccount.id.in_(payload.account_ids))
    accounts = list(session.scalars(account_query.order_by(TgAccount.health_score.desc(), TgAccount.id.asc())))
    requested_account_ids = set(payload.account_ids)
    found_account_ids = {account.id for account in accounts}
    missing_account_ids = sorted(requested_account_ids - found_account_ids)
    account_ids = [account.id for account in accounts]
    setting = get_scheduling_setting(session, tenant_id)
    hour_usage = _usage_counts(session, tenant_id, account_ids, _hour_start(scheduled_at), _hour_start(scheduled_at) + timedelta(hours=1))
    day_start = scheduled_at.replace(hour=0, minute=0, second=0, microsecond=0)
    day_usage = _usage_counts(session, tenant_id, account_ids, day_start, day_start + timedelta(days=1))
    recent_risks = _recent_risks_by_account(session, tenant_id)

    available_accounts: list[dict[str, Any]] = []
    limited_accounts: list[dict[str, Any]] = []
    blocked_accounts: list[dict[str, Any]] = []
    decision_reasons: list[str] = []
    for account in accounts:
        capacity = account_capacity_decision(session, tenant_id=tenant_id, account_id=account.id, scheduled_at=scheduled_at)
        score = _account_score_row(account, setting, capacity, hour_usage, day_usage, recent_risks.get(account.id, ""))
        item = {
            "account_id": score["account_id"],
            "display_name": score["display_name"],
            "risk_level": score["risk_level"],
            "health_score": score["health_score"],
            "proxy_id": score["proxy_id"],
            "proxy_status": score["proxy_status"],
            "proxy_alert_status": score["proxy_alert_status"],
            "reason": score["blocked_reason"] or "可参与",
            "score_reasons": score["score_reasons"],
        }
        if score["can_join_task"]:
            available_accounts.append(item)
        elif score["risk_level"] in {"B", "C", "D"} and not _proxy_blocks_account(account):
            limited_accounts.append(item)
            decision_reasons.append("account_limited")
        else:
            blocked_accounts.append(item)
            decision_reasons.append(_reason_code_for_score(score))
    if missing_account_ids:
        decision_reasons.append("account_missing")
        for account_id in missing_account_ids:
            blocked_accounts.append({
                "account_id": account_id,
                "display_name": f"账号 #{account_id}",
                "risk_level": "E",
                "health_score": 0,
                "proxy_id": None,
                "proxy_status": "",
                "proxy_alert_status": "",
                "reason": "账号不存在或不可见",
                "score_reasons": ["账号不存在、已删除或不属于当前租户"],
            })
    if not accounts and not missing_account_ids:
        decision_reasons.append("no_available_account")

    proxy_decisions, proxy_warnings, proxy_alerts = _preflight_proxy_decisions(session, tenant_id, payload, accounts)
    target_warnings = _preflight_target_warnings(session, tenant_id, payload.target_ids)
    content_warnings = _preflight_content_warnings(session, tenant_id, payload.content_preview)
    decision_reasons.extend([warning["reason_code"] for warning in proxy_alerts if warning.get("reason_code")])
    if target_warnings:
        decision_reasons.append("target_warning")
    if content_warnings:
        decision_reasons.append("content_warning")

    hard_block = bool(blocked_accounts and not available_accounts) or bool(missing_account_ids) or not accounts or any(item.get("blocks") for item in proxy_decisions) or any("不可" in item or "禁止" in item for item in target_warnings)
    decision = "block" if hard_block else "warn" if limited_accounts or proxy_warnings or target_warnings or content_warnings else "allow"
    suggested_actions = _preflight_suggested_actions(decision, blocked_accounts, proxy_alerts, target_warnings, content_warnings)
    return {
        "decision": decision,
        "available_accounts": available_accounts,
        "limited_accounts": limited_accounts,
        "blocked_accounts": blocked_accounts,
        "proxy_decisions": proxy_decisions,
        "proxy_warnings": proxy_warnings,
        "proxy_alerts": proxy_alerts,
        "target_warnings": target_warnings,
        "content_warnings": content_warnings,
        "suggested_actions": suggested_actions,
        "decision_reasons": sorted(set(decision_reasons)),
        "expires_at": _now() + timedelta(seconds=60),
        "trace_id": trace_id,
    }


def risk_control_summary(session: Session, tenant_id: int) -> dict[str, Any]:
    setting = get_scheduling_setting(session, tenant_id)
    now = _now()
    accounts = list(
        session.scalars(
            select(TgAccount)
            .options(selectinload(TgAccount.pool), selectinload(TgAccount.proxy))
            .where(TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None))
            .order_by(TgAccount.health_score.desc(), TgAccount.id.asc())
        )
    )
    account_ids = [account.id for account in accounts]
    hour_usage = _usage_counts(session, tenant_id, account_ids, _hour_start(now), _hour_start(now) + timedelta(hours=1))
    day_usage = _usage_counts(session, tenant_id, account_ids, now.replace(hour=0, minute=0, second=0, microsecond=0), now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
    recent_risks = _recent_risks_by_account(session, tenant_id)

    account_scores = []
    disposition_queue = []
    for account in accounts:
        capacity = account_capacity_decision(session, tenant_id=tenant_id, account_id=account.id, scheduled_at=now)
        recent_risk = recent_risks.get(account.id, "")
        score = _account_score_row(account, setting, capacity, hour_usage, day_usage, recent_risk)
        account_scores.append(score)
        disposition_queue.extend(_account_dispositions(account, score, capacity, recent_risk))

    hit_records = _risk_hit_records(session, tenant_id)
    disposition_queue.extend(_hit_dispositions(hit_records))
    disposition_queue = _dedupe_queue(disposition_queue)
    disposition_queue.sort(key=lambda item: (_severity_sort(item["severity"]), item.get("occurred_at") or now), reverse=True)

    proxy_alerts = list_proxy_alerts(session, tenant_id)
    disposition_queue.extend(_proxy_dispositions(proxy_alerts))
    disposition_queue = _dedupe_queue(disposition_queue)
    disposition_queue.sort(key=lambda item: (_severity_sort(item["severity"]), item.get("occurred_at") or now), reverse=True)
    overview = _overview(setting, account_scores, disposition_queue, hit_records, now, proxy_alerts)
    return {
        "overview": overview,
        "global_policy": _global_policy(setting),
        "account_scores": account_scores,
        "disposition_queue": disposition_queue[:80],
        "hit_records": hit_records[:80],
        "proxy_alerts": proxy_alerts,
    }


def _account_score_row(
    account: TgAccount,
    setting: SchedulingSetting,
    capacity: Any,
    hour_usage: dict[int, int],
    day_usage: dict[int, int],
    recent_risk: str,
) -> dict[str, Any]:
    score_reasons = _score_reasons(account, capacity, recent_risk)
    proxy_blocked = _proxy_blocks_account(account)
    status_blocked = account.status in BLOCKED_ACCOUNT_STATUSES
    capacity_blocked = not bool(capacity.available)
    adjusted_score = max(0, min(100, float(account.health_score or 0) + _proxy_score_delta(account)))
    risk_level = _risk_level(adjusted_score, status_blocked=status_blocked or proxy_blocked, capacity_blocked=capacity_blocked)
    blocked_reason = _blocked_reason(account, capacity, recent_risk)
    if proxy_blocked and not blocked_reason:
        blocked_reason = _proxy_risk_reason(account)
    return {
        "account_id": account.id,
        "display_name": account.display_name,
        "username": account.username,
        "phone_masked": account.phone_masked,
        "pool_name": account.pool_name,
        "login_status": account.status,
        "health_score": round(adjusted_score, 1),
        "risk_level": risk_level,
        "current_policy": _policy_for_level(risk_level, capacity_blocked),
        "hour_usage": hour_usage.get(account.id, 0),
        "hour_limit": int(setting.default_account_hour_limit or 0),
        "day_usage": day_usage.get(account.id, 0),
        "day_limit": int(setting.default_account_day_limit or 0),
        "cooldown_until": capacity.defer_until if capacity_blocked else None,
        "recent_risk": recent_risk,
        "blocked_reason": blocked_reason,
        "score_reasons": score_reasons,
        "proxy_id": account.proxy_id,
        "proxy_name": account.proxy_name,
        "proxy_local_address": account.proxy_local_address,
        "proxy_status": account.proxy_status,
        "proxy_alert_status": account.proxy_alert_status,
        "proxy_risk_reason": _proxy_risk_reason(account),
        "can_join_task": account.status == AccountStatus.ACTIVE.value and risk_level in {"A", "B", "C"} and not capacity_blocked and not proxy_blocked,
    }


def _account_dispositions(account: TgAccount, score: dict[str, Any], capacity: Any, recent_risk: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if account.status in LOGIN_DISPOSITION_STATUSES:
        items.append(_queue_item(f"login:{account.id}", "待完成登录", "warning", account, "账号登录流程未完成", "去账号中心完成登录", account.created_at))
    elif account.status in {AccountStatus.NEED_RELOGIN.value, AccountStatus.SESSION_EXPIRED.value}:
        items.append(_queue_item(f"session:{account.id}", "Session 失效", "critical", account, account.status, "去账号中心重新登录", account.last_active_at))
    elif account.status in {AccountStatus.LIMITED.value, AccountStatus.SUSPECTED_BANNED.value, AccountStatus.BANNED.value}:
        items.append(_queue_item(f"limited:{account.id}", "账号受限", "critical", account, account.status, "暂停账号并替换任务账号", account.last_active_at))
    elif account.status in {AccountStatus.DISABLED.value, AccountStatus.ERROR.value}:
        items.append(_queue_item(f"blocked:{account.id}", "异常账号", "warning", account, account.status, "检查账号状态并决定是否恢复", account.last_active_at))

    if score["risk_level"] in {"D", "E"} and account.status == AccountStatus.ACTIVE.value:
        items.append(_queue_item(f"score:{account.id}", "账号评分过低", "warning", account, f"健康分 {score['health_score']}，{recent_risk or '近期稳定性不足'}", "降频、冷却或移入观察分组", account.last_active_at))
    if not capacity.available:
        items.append(_queue_item(f"capacity:{account.id}:{capacity.reason_code}", "账号容量受限", "info", account, capacity.reason, "优先转派，无法转派则延后执行", capacity.defer_until))
    if _proxy_blocks_account(account):
        items.append(_queue_item(f"proxy:{account.id}:{account.proxy_id or 'missing'}", "代理异常", "critical", account, _proxy_risk_reason(account), "检查代理或切换账号绑定代理", _now()))
    return items


def _hit_dispositions(hit_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for hit in hit_records:
        if hit["policy"] not in {FailureType.FLOOD_WAIT.value, FailureType.ACCOUNT_LIMITED.value, FailureType.ACCOUNT_UNAVAILABLE.value, FailureType.GROUP_PERMISSION_DENIED.value, FailureType.CHANNEL_POST_DENIED.value}:
            continue
        item_type = "FloodWait 频繁" if hit["policy"] == FailureType.FLOOD_WAIT.value else "策略命中待处置"
        items.append({
            "key": f"hit:{hit['key']}",
            "item_type": item_type,
            "severity": hit["severity"],
            "account_id": hit["account_id"],
            "account_name": hit["account_name"],
            "target": hit["target"],
            "reason": hit["detail"] or hit["policy"],
            "suggested_action": "查看命中记录并按策略换号、延后或暂停",
            "occurred_at": hit["occurred_at"],
            "status": "待处理",
        })
    return items


def _proxy_dispositions(proxy_alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for alert in proxy_alerts:
        if alert["alert_status"] not in {"alerting", "acknowledged", "disabled"}:
            continue
        items.append({
            "key": f"proxy-alert:{alert['id']}",
            "item_type": "代理异常",
            "severity": alert["severity"],
            "account_id": None,
            "account_name": "",
            "target": alert["local_address"],
            "reason": alert["last_error"] or alert["reason_code"] or alert["alert_type"],
            "suggested_action": alert["suggested_action"],
            "occurred_at": alert["occurred_at"],
            "status": "待处理" if alert["alert_status"] == "alerting" else alert["alert_status"],
        })
    return items


def _queue_item(key: str, item_type: str, severity: str, account: TgAccount, reason: str, action: str, occurred_at: datetime | None) -> dict[str, Any]:
    return {
        "key": key,
        "item_type": item_type,
        "severity": severity,
        "account_id": account.id,
        "account_name": account.display_name,
        "target": "",
        "reason": reason,
        "suggested_action": action,
        "occurred_at": occurred_at,
        "status": "待处理",
    }


def _overview(
    setting: SchedulingSetting,
    account_scores: list[dict[str, Any]],
    disposition_queue: list[dict[str, Any]],
    hit_records: list[dict[str, Any]],
    now: datetime,
    proxy_alerts: list[dict[str, Any]],
) -> dict[str, Any]:
    blocked = sum(1 for item in account_scores if item["risk_level"] == "E")
    cooldown = sum(1 for item in account_scores if item["risk_level"] == "D")
    degraded = sum(1 for item in account_scores if item["risk_level"] in {"B", "C", "D"})
    available = sum(1 for item in account_scores if item["can_join_task"])
    critical_items = sum(1 for item in disposition_queue if item["severity"] == "critical")
    flood_waits = sum(1 for item in hit_records if item["policy"] == FailureType.FLOOD_WAIT.value)
    active_proxy_alerts = sum(1 for item in proxy_alerts if item["alert_status"] in {"alerting", "disabled", "acknowledged"})
    current_level = _current_level(available, blocked, cooldown, critical_items, flood_waits)
    return {
        "current_level": current_level,
        "level_detail": _level_detail(current_level),
        "quiet_active": _quiet_active(setting, now),
        "metrics": [
            _metric("available_accounts", "可用账号", available, "在线且通过风控", "normal" if available else "warning"),
            _metric("degraded_accounts", "降频账号", degraded, "B/C/D 级账号", "warning" if degraded else "normal"),
            _metric("blocked_accounts", "阻塞账号", blocked, "登录、Session、受限或封禁", "critical" if blocked else "normal"),
            _metric("pending_dispositions", "待处理处置项", len(disposition_queue), "账号、容量与命中记录", "critical" if critical_items else "warning" if disposition_queue else "normal"),
            _metric("recent_flood_wait", "最近 FloodWait", flood_waits, "近 24 小时命中", "warning" if flood_waits else "normal"),
            _metric("proxy_alerts", "代理告警", active_proxy_alerts, "本地代理资源", "critical" if active_proxy_alerts else "normal"),
        ],
    }


def _global_policy(setting: SchedulingSetting) -> dict[str, Any]:
    return {
        "jitter_min_seconds": setting.jitter_min_seconds,
        "jitter_max_seconds": setting.jitter_max_seconds,
        "batch_interval_seconds": setting.batch_interval_seconds,
        "respect_send_window": setting.respect_send_window,
        "quiet_hours_enabled": setting.quiet_hours_enabled,
        "quiet_start": setting.quiet_start,
        "quiet_end": setting.quiet_end,
        "quiet_timezone": setting.quiet_timezone,
        "default_max_retries": setting.default_max_retries,
        "default_retry_delay_seconds": setting.default_retry_delay_seconds,
        "default_retry_backoff": setting.default_retry_backoff,
        "default_on_account_banned": setting.default_on_account_banned,
        "default_on_api_rate_limit": setting.default_on_api_rate_limit,
        "default_on_content_rejected": setting.default_on_content_rejected,
        "default_account_hour_limit": setting.default_account_hour_limit,
        "default_account_day_limit": setting.default_account_day_limit,
        "default_account_cooldown_seconds": setting.default_account_cooldown_seconds,
        "updated_at": setting.updated_at,
    }


def _usage_counts(session: Session, tenant_id: int, account_ids: list[int], start: datetime, end: datetime) -> dict[int, int]:
    if not account_ids:
        return {}
    counts: defaultdict[int, int] = defaultdict(int)
    action_at = func.coalesce(Action.executed_at, Action.scheduled_at)
    for account_id, count in session.execute(
        select(Action.account_id, func.count(Action.id))
        .where(
            Action.tenant_id == tenant_id,
            Action.account_id.in_(account_ids),
            Action.status.in_(ACTION_OCCUPIED_STATUSES),
            action_at >= start,
            action_at < end,
        )
        .group_by(Action.account_id)
    ):
        if account_id is not None:
            counts[int(account_id)] += int(count or 0)

    message_account_id = func.coalesce(MessageTask.account_id, MessageTask.preferred_account_id)
    message_at = func.coalesce(MessageTask.sent_at, MessageTask.scheduled_at)
    for account_id, count in session.execute(
        select(message_account_id, func.count(MessageTask.id))
        .where(
            MessageTask.tenant_id == tenant_id,
            message_account_id.in_(account_ids),
            MessageTask.status.in_(MESSAGE_TASK_OCCUPIED_STATUSES),
            message_at >= start,
            message_at < end,
        )
        .group_by(message_account_id)
    ):
        if account_id is not None:
            counts[int(account_id)] += int(count or 0)
    return dict(counts)


def _recent_risks_by_account(session: Session, tenant_id: int) -> dict[int, str]:
    risks: dict[int, str] = {}
    since = _now() - timedelta(days=7)
    rows = session.execute(
        select(MessageTaskAttempt.account_id, MessageTaskAttempt.failure_type, MessageTaskAttempt.detail, MessageTaskAttempt.created_at)
        .where(MessageTaskAttempt.tenant_id == tenant_id, MessageTaskAttempt.account_id.is_not(None), MessageTaskAttempt.status.in_(FAILED_STATUSES), MessageTaskAttempt.created_at >= since)
        .order_by(MessageTaskAttempt.created_at.desc())
        .limit(100)
    )
    for account_id, failure_type, detail, _created_at in rows:
        if account_id and account_id not in risks:
            risks[int(account_id)] = str(failure_type or detail or "最近执行失败")

    rows = session.execute(
        select(Action.account_id, Action.result, Action.executed_at, Action.scheduled_at)
        .where(Action.tenant_id == tenant_id, Action.account_id.is_not(None), Action.status.in_(FAILED_STATUSES), Action.scheduled_at >= since)
        .order_by(func.coalesce(Action.executed_at, Action.scheduled_at).desc())
        .limit(100)
    )
    for account_id, result, _executed_at, _scheduled_at in rows:
        if account_id and account_id not in risks:
            risks[int(account_id)] = _result_reason(result)
    return risks


def _risk_hit_records(session: Session, tenant_id: int) -> list[dict[str, Any]]:
    accounts = {account.id: account.display_name for account in session.scalars(select(TgAccount).where(TgAccount.tenant_id == tenant_id))}
    records: list[dict[str, Any]] = []
    since = _now() - timedelta(days=1)

    for action in session.scalars(
        select(Action)
        .where(Action.tenant_id == tenant_id, Action.status.in_(FAILED_STATUSES), Action.scheduled_at >= since)
        .order_by(func.coalesce(Action.executed_at, Action.scheduled_at).desc())
        .limit(50)
    ):
        policy = _result_policy(action.result)
        records.append(_hit_record(
            key=f"action:{action.id}",
            source="任务中心",
            account_id=action.account_id,
            accounts=accounts,
            task_id=action.task_id,
            target=str((action.payload or {}).get("target") or (action.payload or {}).get("target_id") or ""),
            policy=policy,
            action=_action_for_policy(policy),
            detail=_result_reason(action.result),
            occurred_at=action.executed_at or action.scheduled_at,
        ))

    for task in session.scalars(
        select(MessageTask)
        .where(MessageTask.tenant_id == tenant_id, MessageTask.status == TaskStatus.FAILED.value, MessageTask.scheduled_at >= since)
        .order_by(func.coalesce(MessageTask.sent_at, MessageTask.scheduled_at).desc())
        .limit(50)
    ):
        account_id = task.account_id or task.preferred_account_id
        records.append(_hit_record(
            key=f"message:{task.id}",
            source="消息发送",
            account_id=account_id,
            accounts=accounts,
            task_id=str(task.id),
            target=task.target_display or task.target_peer_id or "",
            policy=task.failure_type or FailureType.UNKNOWN.value,
            action=_action_for_policy(task.failure_type or ""),
            detail=task.failure_detail or task.failure_type or "消息发送失败",
            occurred_at=task.sent_at or task.scheduled_at,
        ))

    for attempt in session.scalars(
        select(OperationTaskAttempt)
        .where(OperationTaskAttempt.tenant_id == tenant_id, OperationTaskAttempt.status.in_(FAILED_STATUSES), OperationTaskAttempt.scheduled_at >= since)
        .order_by(func.coalesce(OperationTaskAttempt.executed_at, OperationTaskAttempt.scheduled_at).desc())
        .limit(50)
    ):
        records.append(_hit_record(
            key=f"operation:{attempt.id}",
            source="运营目标",
            account_id=attempt.account_id,
            accounts=accounts,
            task_id=str(attempt.task_id),
            target="",
            policy=attempt.failure_type or FailureType.UNKNOWN.value,
            action=_action_for_policy(attempt.failure_type or ""),
            detail=attempt.failure_detail or attempt.failure_type or "执行失败",
            occurred_at=attempt.executed_at or attempt.scheduled_at,
        ))

    for record in session.scalars(
        select(ManualOperationRecord)
        .where(ManualOperationRecord.tenant_id == tenant_id, ManualOperationRecord.status.in_(FAILED_STATUSES), ManualOperationRecord.created_at >= since)
        .order_by(ManualOperationRecord.created_at.desc())
        .limit(50)
    ):
        records.append(_hit_record(
            key=f"manual:{record.id}",
            source="人工发送",
            account_id=record.account_id,
            accounts=accounts,
            task_id=str(record.id),
            target=str(record.target_id or ""),
            policy=record.failure_type or FailureType.UNKNOWN.value,
            action=_action_for_policy(record.failure_type or ""),
            detail=record.failure_detail or record.failure_type or "人工操作失败",
            occurred_at=record.created_at,
        ))

    records.sort(key=lambda item: item.get("occurred_at") or datetime.min, reverse=True)
    return records


def _hit_record(
    *,
    key: str,
    source: str,
    account_id: int | None,
    accounts: dict[int, str],
    task_id: str,
    target: str,
    policy: str,
    action: str,
    detail: str,
    occurred_at: datetime | None,
) -> dict[str, Any]:
    severity = "critical" if policy in {FailureType.ACCOUNT_LIMITED.value, FailureType.ACCOUNT_UNAVAILABLE.value, FailureType.FLOOD_WAIT.value} else "warning"
    return {
        "key": key,
        "source": source,
        "severity": severity,
        "account_id": account_id,
        "account_name": accounts.get(account_id or 0, ""),
        "task_id": task_id,
        "target": target,
        "policy": policy or FailureType.UNKNOWN.value,
        "action": action,
        "detail": detail,
        "occurred_at": occurred_at,
    }


def _risk_level(score: float, *, status_blocked: bool, capacity_blocked: bool) -> str:
    if status_blocked:
        return "E"
    if capacity_blocked:
        return "D"
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 30:
        return "D"
    return "E"


def _policy_for_level(level: str, capacity_blocked: bool) -> str:
    if capacity_blocked:
        return "转派或延后"
    return {
        "A": "标准节奏",
        "B": "轻微降频",
        "C": "保守低频",
        "D": "冷却恢复",
        "E": "阻塞处置",
    }.get(level, "阻塞处置")


def _blocked_reason(account: TgAccount, capacity: Any, recent_risk: str) -> str:
    if account.status in BLOCKED_ACCOUNT_STATUSES:
        return account.status
    if not capacity.available:
        return capacity.reason
    if account.health_score < 55:
        return recent_risk or "健康分低于任务准入线"
    return ""


def _score_reasons(account: TgAccount, capacity: Any, recent_risk: str) -> list[str]:
    reasons: list[str] = []
    if account.status != AccountStatus.ACTIVE.value:
        reasons.append(f"登录状态：{account.status}")
    if not capacity.available:
        reasons.append(capacity.reason)
    proxy_reason = _proxy_risk_reason(account)
    if proxy_reason:
        reasons.append(proxy_reason)
    if recent_risk:
        reasons.append(f"最近风险：{recent_risk}")
    if not reasons:
        reasons.append("账号状态、容量和代理检查均未命中阻塞")
    return reasons[:6]


def _proxy_score_delta(account: TgAccount) -> int:
    proxy = account.proxy
    if proxy is None:
        return 0
    if proxy.status == "disabled" or proxy.alert_status == "disabled":
        return -100
    if proxy.alert_status == "alerting" or proxy.status == "unhealthy":
        return -60
    if proxy.alert_status in {"observing", "acknowledged"}:
        return -15
    return 0


def _proxy_blocks_account(account: TgAccount) -> bool:
    proxy = account.proxy
    if proxy is None:
        return False
    return proxy.status in {"disabled", "unhealthy"} or proxy.alert_status in {"alerting", "disabled"}


def _proxy_risk_reason(account: TgAccount) -> str:
    proxy = account.proxy
    if proxy is None:
        return ""
    if proxy.status == "disabled" or proxy.alert_status == "disabled":
        return "代理已禁用"
    if proxy.alert_status == "alerting":
        return proxy.last_error or "代理存在未处理告警"
    if proxy.status == "unhealthy":
        return proxy.last_error or "代理健康检查异常"
    if proxy.alert_status in {"observing", "acknowledged"}:
        return proxy.last_error or "代理处于观察/处理中"
    return ""


def _current_level(available: int, blocked: int, cooldown: int, critical_items: int, flood_waits: int) -> str:
    if available == 0 or critical_items >= 3:
        return "暂停"
    if blocked or cooldown or flood_waits >= 3:
        return "收紧"
    if flood_waits or critical_items:
        return "注意"
    return "正常"


def _level_detail(level: str) -> str:
    return {
        "正常": "账号池可按默认策略运行",
        "注意": "存在少量风险命中，需要观察",
        "收紧": "存在阻塞账号或限速压力，任务应优先换号或降频",
        "暂停": "可用账号不足或严重风险较多，应暂停新执行项",
    }[level]


def _quiet_active(setting: SchedulingSetting, now: datetime) -> bool:
    if not setting.quiet_hours_enabled:
        return False
    try:
        start_hour, start_minute = [int(part) for part in setting.quiet_start.split(":", 1)]
        end_hour, end_minute = [int(part) for part in setting.quiet_end.split(":", 1)]
    except ValueError:
        return False
    current = now.hour * 60 + now.minute
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def _metric(key: str, label: str, value: int | float | str, detail: str, status: str) -> dict[str, Any]:
    return {"key": key, "label": label, "value": value, "detail": detail, "status": status}


def _severity_sort(severity: str) -> int:
    return {"critical": 3, "warning": 2, "info": 1}.get(severity, 0)


def _result_policy(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return FailureType.UNKNOWN.value
    return str(result.get("failure_type") or result.get("reason_code") or result.get("error_code") or FailureType.UNKNOWN.value)


def _result_reason(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return "执行失败"
    return str(result.get("detail") or result.get("failure_detail") or result.get("reason") or result.get("error") or _result_policy(result))


def _action_for_policy(policy: str) -> str:
    if policy == FailureType.FLOOD_WAIT.value:
        return "等待重试或转派"
    if policy in {FailureType.ACCOUNT_UNAVAILABLE.value, FailureType.ACCOUNT_LIMITED.value}:
        return "暂停账号并换号"
    if policy in {FailureType.GROUP_PERMISSION_DENIED.value, FailureType.CHANNEL_POST_DENIED.value}:
        return "处理目标权限或替换账号"
    if policy == FailureType.CONTENT_REJECTED.value:
        return "跳过或改写重试"
    return "记录并按失败策略处理"


def _hour_start(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def _dedupe_queue(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for item in items:
        deduped.setdefault(item["key"], item)
    return list(deduped.values())


def _require_proxy(session: Session, tenant_id: int, proxy_id: int | None) -> AccountProxy:
    if proxy_id is None:
        raise ValueError("proxy_id required")
    proxy = session.get(AccountProxy, proxy_id)
    if not proxy or proxy.tenant_id != tenant_id:
        raise ValueError("account proxy not found")
    return proxy


def _require_account(session: Session, tenant_id: int, account_id: int) -> TgAccount:
    account = session.get(TgAccount, account_id)
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        raise ValueError("account not found")
    return account


def _proxy_payload(proxy: AccountProxy, bound_count: int, *, trace_id: str = "") -> dict[str, Any]:
    return {
        "id": proxy.id,
        "tenant_id": proxy.tenant_id,
        "name": proxy.name,
        "protocol": proxy.protocol,
        "host": proxy.host,
        "port": proxy.port,
        "username": proxy.username,
        "status": proxy.status,
        "alert_status": proxy.alert_status,
        "check_interval_seconds": proxy.check_interval_seconds,
        "timeout_ms": proxy.timeout_ms,
        "max_bound_accounts": proxy.max_bound_accounts,
        "max_concurrent_sessions": proxy.max_concurrent_sessions,
        "last_check_at": proxy.last_check_at,
        "last_error": proxy.last_error,
        "disabled_reason": proxy.disabled_reason,
        "notes": proxy.notes,
        "local_address": proxy.local_address,
        "bound_account_count": bound_count,
        "created_at": proxy.created_at,
        "updated_at": proxy.updated_at,
        "trace_id": trace_id,
    }


def _proxy_alert_payload(alert: ProxyAlert) -> dict[str, Any]:
    proxy = alert.proxy
    return {
        "id": alert.id,
        "proxy_id": alert.proxy_id,
        "name": proxy.name if proxy else f"proxy_{alert.proxy_id}",
        "local_address": proxy.local_address if proxy else "",
        "alert_status": alert.status,
        "severity": alert.severity,
        "alert_type": alert.alert_type,
        "reason_code": alert.reason_code,
        "bound_accounts": len(alert.affected_account_ids or []),
        "last_error": proxy.last_error if proxy else alert.reason_code,
        "suggested_action": alert.suggested_action,
        "occurred_at": alert.last_seen_at,
    }


def _proxy_bound_counts(session: Session, tenant_id: int, proxy_ids: list[int]) -> dict[int, int]:
    if not proxy_ids:
        return {}
    rows = session.execute(
        select(TgAccount.proxy_id, func.count(TgAccount.id))
        .where(TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None), TgAccount.proxy_id.in_(proxy_ids))
        .group_by(TgAccount.proxy_id)
    )
    return {int(proxy_id): int(count) for proxy_id, count in rows if proxy_id is not None}


def _probe_proxy(proxy: AccountProxy) -> tuple[str, int, str, str]:
    started = time.perf_counter()
    try:
        with socket.create_connection((proxy.host, int(proxy.port)), timeout=max(0.1, proxy.timeout_ms / 1000)):
            pass
        return "healthy", int((time.perf_counter() - started) * 1000), "", ""
    except OSError as exc:
        return "unreachable", int((time.perf_counter() - started) * 1000), "proxy_unreachable", str(exc)


def _recover_proxy_alerts(session: Session, proxy: AccountProxy) -> None:
    now = _now()
    for alert in session.scalars(
        select(ProxyAlert).where(
            ProxyAlert.tenant_id == proxy.tenant_id,
            ProxyAlert.proxy_id == proxy.id,
            ProxyAlert.status.in_(["alerting", "acknowledged", "ignored"]),
        )
    ):
        alert.status = "recovered"
        alert.recovered_at = now


def _proxy_binding_warnings(session: Session, tenant_id: int, proxy: AccountProxy, *, exclude_account_id: int | None = None) -> list[str]:
    warnings: list[str] = []
    if proxy.status in {"disabled", "unhealthy"} or proxy.alert_status in {"alerting", "disabled"}:
        warnings.append(_proxy_status_reason(proxy))
    if proxy.max_bound_accounts > 0:
        count_query = select(func.count(TgAccount.id)).where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.proxy_id == proxy.id,
            TgAccount.deleted_at.is_(None),
        )
        if exclude_account_id is not None:
            count_query = count_query.where(TgAccount.id != exclude_account_id)
        bound_count = int(session.scalar(count_query) or 0)
        if bound_count >= proxy.max_bound_accounts:
            warnings.append(f"代理绑定账号数已达上限 {proxy.max_bound_accounts}")
    return warnings


def _proxy_status_reason(proxy: AccountProxy) -> str:
    if proxy.status == "disabled" or proxy.alert_status == "disabled":
        return "代理已禁用"
    if proxy.status == "unhealthy":
        return proxy.last_error or "代理健康检查异常"
    if proxy.alert_status == "alerting":
        return proxy.last_error or "代理存在未处理告警"
    return f"代理状态异常：{proxy.status}/{proxy.alert_status}"


def _upsert_proxy_alert(session: Session, proxy: AccountProxy, *, severity: str, alert_type: str, reason_code: str, suggested_action: str) -> ProxyAlert:
    affected = list(session.scalars(select(TgAccount.id).where(TgAccount.tenant_id == proxy.tenant_id, TgAccount.proxy_id == proxy.id, TgAccount.deleted_at.is_(None))))
    alert = session.scalar(
        select(ProxyAlert).where(
            ProxyAlert.tenant_id == proxy.tenant_id,
            ProxyAlert.proxy_id == proxy.id,
            ProxyAlert.status.in_(["alerting", "acknowledged", "ignored"]),
        ).order_by(ProxyAlert.last_seen_at.desc())
    )
    if not alert:
        alert = ProxyAlert(tenant_id=proxy.tenant_id, proxy_id=proxy.id, first_seen_at=_now())
        session.add(alert)
        session.flush()
    alert.severity = severity
    alert.status = "alerting" if alert.status not in {"acknowledged", "ignored"} else alert.status
    alert.alert_type = alert_type
    alert.reason_code = reason_code
    alert.last_seen_at = _now()
    alert.affected_account_ids = affected
    alert.suggested_action = suggested_action
    return alert


def _action_count_for_account(session: Session, tenant_id: int, account_id: int, status: str) -> int:
    return int(session.scalar(select(func.count(Action.id)).where(Action.tenant_id == tenant_id, Action.account_id == account_id, Action.status == status)) or 0)


def _preflight_proxy_decisions(session: Session, tenant_id: int, payload: RiskPreflightRequest, accounts: list[TgAccount]) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    proxy_ids = set(payload.proxy_ids)
    proxy_ids.update(account.proxy_id for account in accounts if account.proxy_id)
    proxies = {proxy.id: proxy for proxy in session.scalars(select(AccountProxy).where(AccountProxy.tenant_id == tenant_id, AccountProxy.id.in_(proxy_ids))) } if proxy_ids else {}
    decisions: list[dict[str, Any]] = []
    warnings: list[str] = []
    alerts: list[dict[str, Any]] = []
    for proxy_id in sorted(proxy_ids):
        proxy = proxies.get(proxy_id)
        if not proxy:
            decisions.append({"proxy_id": proxy_id, "status": "missing", "alert_status": "alerting", "blocks": True, "reason_code": "proxy_missing", "suggested_action": "重新绑定有效本地代理"})
            alerts.append({"proxy_id": proxy_id, "severity": "critical", "reason_code": "proxy_missing"})
            continue
        blocks = proxy.status in {"disabled", "unhealthy"} or proxy.alert_status in {"alerting", "disabled"}
        reason_code = "proxy_disabled" if proxy.status == "disabled" or proxy.alert_status == "disabled" else "proxy_alert_active" if blocks else ""
        if proxy.alert_status in {"observing", "acknowledged"}:
            warnings.append(f"{proxy.name} 处于{proxy.alert_status}，系统将降低发送强度")
            reason_code = reason_code or "proxy_observing"
        decision = {
            "proxy_id": proxy.id,
            "name": proxy.name,
            "local_address": proxy.local_address,
            "status": proxy.status,
            "alert_status": proxy.alert_status,
            "blocks": blocks,
            "reason_code": reason_code,
            "suggested_action": "检查代理或切换 proxy_id" if blocks else "保持观察" if proxy.alert_status != "normal" else "",
        }
        decisions.append(decision)
        if blocks:
            alerts.append({"proxy_id": proxy.id, "severity": "critical", "reason_code": reason_code, "alert_status": proxy.alert_status})
    if any(account.proxy_id is None for account in accounts):
        warnings.append("存在未绑定本地代理的账号，建议先在账号中心绑定后再执行高频发送")
    return decisions, warnings, alerts


def _preflight_target_warnings(session: Session, tenant_id: int, target_ids: list[int]) -> list[str]:
    if not target_ids:
        return []
    warnings: list[str] = []
    for target in session.scalars(select(OperationTarget).where(OperationTarget.tenant_id == tenant_id, OperationTarget.id.in_(target_ids))):
        if not target.can_send:
            warnings.append(f"{target.title} 当前不可发送")
        if target.auth_status not in {"已授权运营", "AUTHORIZED"}:
            warnings.append(f"{target.title} 授权状态为 {target.auth_status}")
    missing = set(target_ids) - {target.id for target in session.scalars(select(OperationTarget).where(OperationTarget.tenant_id == tenant_id, OperationTarget.id.in_(target_ids)))}
    warnings.extend([f"目标 #{target_id} 不存在或不可见" for target_id in sorted(missing)])
    return warnings


def _preflight_content_warnings(session: Session, tenant_id: int, content: str) -> list[str]:
    cleaned = str(content or "").strip()
    if not cleaned:
        return []
    warnings: list[str] = []
    lowered = cleaned.lower()
    for rule in tenant_keyword_rules(session, tenant_id):
        keyword = rule.keyword.strip()
        if keyword and rule.match_type == "contains" and keyword.lower() in lowered:
            warnings.append(f"内容命中关键词：{keyword}")
    return warnings[:5]


def _reason_code_for_score(score: dict[str, Any]) -> str:
    reason = score.get("blocked_reason") or ""
    if "代理" in reason:
        return "proxy_missing" if "未配置" in reason else "proxy_alert_active"
    if "Session" in reason or "登录" in reason:
        return "account_login_required"
    if "容量" in reason or "上限" in reason:
        return "account_limit"
    return "account_blocked"


def _preflight_suggested_actions(decision: str, blocked_accounts: list[dict[str, Any]], proxy_alerts: list[dict[str, Any]], target_warnings: list[str], content_warnings: list[str]) -> list[str]:
    actions: list[str] = []
    if decision == "allow":
        return ["允许提交，执行前仍会重新校验"]
    if blocked_accounts:
        actions.append("更换可用账号或去账号中心处理登录/代理问题")
    if proxy_alerts:
        actions.append("进入风控中心代理告警页检查代理或切换 proxy_id")
    if target_warnings:
        actions.append("检查运营目标授权和账号目标权限")
    if content_warnings:
        actions.append("修改内容或使用规则中心改写建议")
    return actions or ["调整账号、目标或发送时间后重新预检查"]


def _trace_id() -> str:
    return str(uuid4())


__all__ = [
    "bind_account_proxy",
    "bind_accounts_proxy_batch",
    "check_account_proxy",
    "create_account_proxy",
    "disable_account_proxy",
    "list_account_proxies",
    "list_proxy_alerts",
    "risk_preflight",
    "risk_control_summary",
    "update_global_policy",
    "update_account_proxy",
    "update_proxy_alert_status",
]
