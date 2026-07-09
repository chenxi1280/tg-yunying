from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import SearchRankDeboostAlert
from app.models.search_rank_deboost_alert import (
    RANK_DEBOOST_ALERT_ACCOUNT_ISOLATION_VIOLATION,
    RANK_DEBOOST_ALERT_ALL_EXEMPT_CLICKS,
    RANK_DEBOOST_ALERT_EXEMPT_GROUP_MISSING,
    RANK_DEBOOST_ALERT_GROUP_IP_DRIFT,
    RANK_DEBOOST_ALERT_JOIN_BUTTON_VIOLATION,
    RANK_DEBOOST_ALERT_NODE_UNREACHABLE,
)
from app.services._common import _now


def record_rank_deboost_alert(
    session: Session,
    *,
    tenant_id: int,
    alert_type: str,
    severity: str = "warning",
    task_id: str = "",
    action_id: str = "",
    account_id: int | None = None,
    context: dict[str, Any] | None = None,
    reason_code: str = "",
    detail: str = "",
) -> SearchRankDeboostAlert:
    """写入一条降权任务风控告警记录。

    每次 occurrence 写一行，便于追踪频次。status 默认 alerting，
    后续可由风控中心或运维接口标记 acknowledged/recovered。
    """
    alert = SearchRankDeboostAlert(
        tenant_id=tenant_id,
        alert_type=alert_type,
        severity=severity,
        task_id=task_id,
        action_id=action_id,
        account_id=account_id,
        context=context or {},
        reason_code=reason_code,
        detail=detail,
        status="alerting",
        first_seen_at=_now(),
        last_seen_at=_now(),
    )
    session.add(alert)
    session.flush()
    return alert


def record_join_button_violation_alert(
    session: Session,
    *,
    tenant_id: int,
    task_id: str,
    action_id: str,
    account_id: int,
    competitor_username: str = "",
    button_effect: str = "",
) -> SearchRankDeboostAlert:
    """Executor 误点加入按钮时生成告警。"""
    return record_rank_deboost_alert(
        session,
        tenant_id=tenant_id,
        alert_type=RANK_DEBOOST_ALERT_JOIN_BUTTON_VIOLATION,
        severity="critical",
        task_id=task_id,
        action_id=action_id,
        account_id=account_id,
        context={
            "competitor_group_username": competitor_username,
            "button_effect": button_effect,
        },
        reason_code="join_button_violation",
        detail="Executor 自检发现误点加入按钮，已暂停账号并停止 action",
    )


def record_all_exempt_clicks_alert(
    session: Session,
    *,
    tenant_id: int,
    task_id: str,
    action_id: str,
    account_id: int | None = None,
) -> SearchRankDeboostAlert:
    """所有结果都被白名单豁免时生成告警（罕见但需可见）。"""
    return record_rank_deboost_alert(
        session,
        tenant_id=tenant_id,
        alert_type=RANK_DEBOOST_ALERT_ALL_EXEMPT_CLICKS,
        severity="info",
        task_id=task_id,
        action_id=action_id,
        account_id=account_id,
        reason_code="all_exempt_clicks",
        detail="当前搜索结果全部被白名单豁免，无竞争群可点击",
    )


def record_exempt_group_missing_alert(
    session: Session,
    *,
    tenant_id: int,
    task_id: str,
) -> SearchRankDeboostAlert:
    """任务启动时豁免群未预选（占位或记录不存在）时生成告警。"""
    return record_rank_deboost_alert(
        session,
        tenant_id=tenant_id,
        alert_type=RANK_DEBOOST_ALERT_EXEMPT_GROUP_MISSING,
        severity="warning",
        task_id=task_id,
        reason_code="exempt_group_pending_real_search",
        detail="任务启动时豁免群未预选，使用 pending_real_search 占位",
    )


def record_account_isolation_violation_alert(
    session: Session,
    *,
    tenant_id: int,
    task_id: str,
    action_id: str,
    account_id: int,
    violation: str,
) -> SearchRankDeboostAlert:
    """降权账号被其他任务选用（或降权任务选用普通账号）时生成告警。

    violation: 'rank_deboost_account_used_by_other' | 'deboost_task_used_normal_account'
    """
    return record_rank_deboost_alert(
        session,
        tenant_id=tenant_id,
        alert_type=RANK_DEBOOST_ALERT_ACCOUNT_ISOLATION_VIOLATION,
        severity="warning",
        task_id=task_id,
        action_id=action_id,
        account_id=account_id,
        context={"violation": violation},
        reason_code=violation,
        detail="账号组隔离校验失败，已阻断 action 派发",
    )


def record_group_proxy_egress_failure_alert(
    session: Session,
    *,
    tenant_id: int,
    task_id: str,
    action_id: str,
    account_id: int | None,
    binding_id: int,
    binding_active: bool,
    observed_exit_ip: str,
    probe_exit_ip: str,
) -> SearchRankDeboostAlert:
    """分组级代理出口探测失败时生成告警。

    根据 binding 状态与 IP 比对结果分类：
    - binding 不存在/非 active 或 probe 为空 → node_unreachable
    - observed 非空且与 probe 不一致 → group_ip_drift
    - 其他兜底 → node_unreachable
    """
    if binding_active and probe_exit_ip and observed_exit_ip and observed_exit_ip != probe_exit_ip:
        return _record_group_ip_drift_alert(
            session, tenant_id, task_id, action_id, account_id, binding_id, observed_exit_ip, probe_exit_ip
        )
    return _record_node_unreachable_alert(
        session, tenant_id, task_id, action_id, account_id, binding_id, binding_active, observed_exit_ip, probe_exit_ip
    )


def _record_group_ip_drift_alert(
    session: Session,
    tenant_id: int,
    task_id: str,
    action_id: str,
    account_id: int | None,
    binding_id: int,
    observed_exit_ip: str,
    probe_exit_ip: str,
) -> SearchRankDeboostAlert:
    return record_rank_deboost_alert(
        session,
        tenant_id=tenant_id,
        alert_type=RANK_DEBOOST_ALERT_GROUP_IP_DRIFT,
        severity="critical",
        task_id=task_id,
        action_id=action_id,
        account_id=account_id,
        context={
            "group_proxy_binding_id": binding_id,
            "observed_exit_ip": observed_exit_ip,
            "probe_exit_ip": probe_exit_ip,
        },
        reason_code="group_ip_drift",
        detail=f"分组级共享出口 IP 漂移：observed={observed_exit_ip}, probed={probe_exit_ip}",
    )


def _record_node_unreachable_alert(
    session: Session,
    tenant_id: int,
    task_id: str,
    action_id: str,
    account_id: int | None,
    binding_id: int,
    binding_active: bool,
    observed_exit_ip: str,
    probe_exit_ip: str,
) -> SearchRankDeboostAlert:
    context = {
        "group_proxy_binding_id": binding_id,
        "binding_active": binding_active,
        "observed_exit_ip": observed_exit_ip,
        "probe_exit_ip": probe_exit_ip,
    }
    return record_rank_deboost_alert(
        session,
        tenant_id=tenant_id,
        alert_type=RANK_DEBOOST_ALERT_NODE_UNREACHABLE,
        severity="critical",
        task_id=task_id,
        action_id=action_id,
        account_id=account_id,
        context=context,
        reason_code="group_node_unreachable",
        detail="分组级绑定节点不可达或出口探测失败，禁止回退本机直连",
    )


__all__ = [
    "record_account_isolation_violation_alert",
    "record_all_exempt_clicks_alert",
    "record_exempt_group_missing_alert",
    "record_group_proxy_egress_failure_alert",
    "record_join_button_violation_alert",
    "record_rank_deboost_alert",
]
