from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountGroupProxyBinding,
    AccountProxyBinding,
    ProxyAirportNode,
    ProxyAirportSubscription,
)
from app.models.enums import AccountProxyBindingScope
from app.services._common import _now, audit
from app.services.proxy_airport_accounts import proxy_for_airport_node
from app.services.proxy_group_binding_snapshots import binding_snapshot, rank_deboost_pool_reference_count
from app.services.task_center.search_rank_deboost import (
    assert_account_pool_for_rank_deboost,
    assert_node_available_for_group_binding,
)


def create_group_proxy_binding(
    session: Session,
    *,
    tenant_id: int,
    account_pool_id: int,
    proxy_airport_node_id: int,
    operator: str,
) -> AccountGroupProxyBinding:
    """创建分组级代理绑定。

    校验：
    1. account_pool_id 对应分组 pool_purpose='rank_deboost'
    2. proxy_airport_node_id 节点存在且健康
    3. 节点独占：未被授权槽位级 account_proxy_bindings 绑定
    4. 节点独占：未被其他降权分组绑定
    5. 同一分组同一节点幂等复用；不同节点显式切换并提升 generation
    """
    return create_or_update_rank_deboost_proxy_binding(
        session,
        tenant_id=tenant_id,
        account_pool_id=account_pool_id,
        proxy_airport_node_id=proxy_airport_node_id,
        operator=operator,
        reason="create_group_proxy_binding",
    )


def create_or_update_rank_deboost_proxy_binding(
    session: Session,
    *,
    tenant_id: int,
    account_pool_id: int,
    proxy_airport_node_id: int,
    operator: str,
    reason: str = "",
) -> AccountGroupProxyBinding:
    node = _assert_node_available_for_binding_change(
        session,
        tenant_id=tenant_id,
        account_pool_id=account_pool_id,
        proxy_airport_node_id=proxy_airport_node_id,
    )
    runtime_proxy = proxy_for_airport_node(session, node)
    active = _active_group_binding_for_update(session, tenant_id, account_pool_id)
    if active is not None and _same_binding(active, node.id, runtime_proxy.id):
        return active
    generation = int(active.binding_generation or 1) + 1 if active is not None else 1
    if active is not None:
        _mark_group_binding_unbound(active, reason or "switch_rank_deboost_proxy_binding")
    binding = _new_group_binding(
        tenant_id,
        account_pool_id,
        node,
        operator,
        generation=generation,
        reason=reason,
        runtime_proxy_id=runtime_proxy.id,
    )
    session.add(binding)
    session.flush()
    detail = f"pool={account_pool_id}, node={node.id}, runtime_proxy={runtime_proxy.id}"
    _audit_group_binding(session, binding, operator, "create_group_proxy_binding", detail=detail)
    return binding


def _assert_node_available_for_binding_change(
    session: Session,
    *,
    tenant_id: int,
    account_pool_id: int,
    proxy_airport_node_id: int,
) -> ProxyAirportNode:
    assert_account_pool_for_rank_deboost(session, tenant_id=tenant_id, account_pool_id=account_pool_id)
    node = assert_node_available_for_group_binding(
        session,
        tenant_id=tenant_id,
        proxy_airport_node_id=proxy_airport_node_id,
    )
    _assert_node_not_used_by_other_group(session, tenant_id, account_pool_id, node.id)
    return node


def _active_group_binding_for_update(
    session: Session,
    tenant_id: int,
    account_pool_id: int,
) -> AccountGroupProxyBinding | None:
    stmt = (
        select(AccountGroupProxyBinding)
        .where(
            AccountGroupProxyBinding.tenant_id == tenant_id,
            AccountGroupProxyBinding.account_pool_id == int(account_pool_id),
            AccountGroupProxyBinding.status == "active",
            AccountGroupProxyBinding.unbound_at.is_(None),
        )
        .limit(1)
        .with_for_update()
    )
    return session.scalar(stmt)


def _same_binding(binding: AccountGroupProxyBinding, node_id: int, runtime_proxy_id: int) -> bool:
    return binding.proxy_airport_node_id == node_id and binding.runtime_proxy_id == runtime_proxy_id


def _mark_group_binding_unbound(binding: AccountGroupProxyBinding, reason: str) -> None:
    binding.status = "unbound"
    binding.unbound_at = _now()
    binding.change_reason = reason


def _assert_node_not_used_by_other_group(
    session: Session,
    tenant_id: int,
    account_pool_id: int,
    proxy_airport_node_id: int,
) -> None:
    other_group_binding = session.scalar(
        select(AccountGroupProxyBinding.id).where(
            AccountGroupProxyBinding.tenant_id == tenant_id,
            AccountGroupProxyBinding.proxy_airport_node_id == int(proxy_airport_node_id),
            AccountGroupProxyBinding.status == "active",
            AccountGroupProxyBinding.unbound_at.is_(None),
            AccountGroupProxyBinding.account_pool_id != int(account_pool_id),
        ).limit(1)
    )
    if other_group_binding is not None:
        raise ValueError(f"节点 {proxy_airport_node_id} 已被其他降权分组绑定")


def delete_rank_deboost_proxy_binding(
    session: Session,
    *,
    tenant_id: int,
    account_pool_id: int,
    operator: str,
    reason: str,
) -> AccountGroupProxyBinding:
    if rank_deboost_pool_reference_count(session, tenant_id, account_pool_id) > 0:
        raise ValueError("account_pool_has_running/paused_search_rank_deboost_reference")
    binding = _require_active_group_binding(session, tenant_id, account_pool_id)
    return unbind_group_proxy_binding(session, binding_id=binding.id, reason=reason, operator=operator)


def _new_group_binding(
    tenant_id: int,
    account_pool_id: int,
    node: ProxyAirportNode,
    operator: str,
    *,
    generation: int,
    reason: str = "",
    runtime_proxy_id: int | None = None,
) -> AccountGroupProxyBinding:
    return AccountGroupProxyBinding(
        tenant_id=tenant_id,
        account_pool_id=int(account_pool_id),
        proxy_airport_node_id=node.id,
        runtime_proxy_id=runtime_proxy_id,
        binding_scope=AccountProxyBindingScope.GROUP.value,
        observed_exit_ip=node.observed_exit_ip or "",
        observed_exit_country=node.observed_exit_country or "",
        observed_exit_asn=node.observed_exit_asn or "",
        observed_exit_isp=node.observed_exit_isp or "",
        status="active",
        bound_by=operator,
        bound_at=_now(),
        binding_generation=generation,
        change_reason=reason,
    )


def _audit_group_binding(
    session: Session,
    binding: AccountGroupProxyBinding,
    operator: str,
    action: str,
    *,
    detail: str,
) -> None:
    audit(
        session,
        tenant_id=binding.tenant_id,
        actor=operator,
        action=action,
        target_type="account_group_proxy_binding",
        target_id=str(binding.id),
        detail=detail,
    )
    session.flush()


def get_active_group_binding(
    session: Session,
    *,
    tenant_id: int,
    account_pool_id: int,
) -> AccountGroupProxyBinding | None:
    """获取分组的当前 active 绑定。"""
    return session.scalar(
        select(AccountGroupProxyBinding).where(
            AccountGroupProxyBinding.tenant_id == tenant_id,
            AccountGroupProxyBinding.account_pool_id == int(account_pool_id),
            AccountGroupProxyBinding.status == "active",
            AccountGroupProxyBinding.unbound_at.is_(None),
        ).limit(1)
    )


def unbind_group_proxy_binding(
    session: Session,
    *,
    binding_id: int,
    reason: str,
    operator: str,
) -> AccountGroupProxyBinding:
    """解绑分组级代理绑定。"""
    binding = session.get(AccountGroupProxyBinding, int(binding_id))
    if binding is None:
        raise ValueError("group_proxy_binding_not_found")
    if binding.status != "active" or binding.unbound_at is not None:
        raise ValueError("group_proxy_binding_inactive")

    now = _now()
    binding.status = "unbound"
    binding.unbound_at = now
    binding.change_reason = reason
    session.flush()

    audit(
        session,
        tenant_id=binding.tenant_id,
        actor=operator,
        action="unbind_group_proxy_binding",
        target_type="account_group_proxy_binding",
        target_id=str(binding.id),
        detail=f"reason={reason}",
    )
    session.flush()
    return binding


def failover_group_proxy_binding(
    session: Session,
    *,
    tenant_id: int,
    account_pool_id: int,
    reason: str,
    operator: str,
) -> AccountGroupProxyBinding:
    """故障切换：只允许在同订阅内切换到下一个健康节点。

    旧绑定 unbound，新绑定创建，binding_generation + 1。
    """
    old_binding = _require_active_group_binding(session, tenant_id, account_pool_id)
    from_node = _require_proxy_airport_node(session, old_binding)
    to_node = _require_failover_node(session, tenant_id, from_node)
    now = _now()
    old_binding.status = "unbound"
    old_binding.unbound_at = now
    old_binding.change_reason = reason
    old_binding.last_failover_at = now

    new_binding = _new_group_binding(
        tenant_id,
        account_pool_id,
        to_node,
        operator,
        generation=int(old_binding.binding_generation or 1) + 1,
        reason=reason,
    )
    new_binding.last_failover_at = now
    session.add(new_binding)
    session.flush()
    detail = f"pool={account_pool_id}, from_node={from_node.id}, to_node={to_node.id}, reason={reason}"
    _audit_group_binding(session, new_binding, operator, "failover_group_proxy_binding", detail=detail)
    return new_binding


def _require_active_group_binding(session: Session, tenant_id: int, account_pool_id: int) -> AccountGroupProxyBinding:
    binding = get_active_group_binding(session, tenant_id=tenant_id, account_pool_id=account_pool_id)
    if binding is None:
        raise ValueError(f"分组 {account_pool_id} 无 active 绑定，无法故障切换")
    return binding


def _require_proxy_airport_node(session: Session, binding: AccountGroupProxyBinding) -> ProxyAirportNode:
    node = session.get(ProxyAirportNode, int(binding.proxy_airport_node_id))
    if node is None:
        raise ValueError("proxy_airport_node 不存在")
    return node


def _require_failover_node(session: Session, tenant_id: int, from_node: ProxyAirportNode) -> ProxyAirportNode:
    node = _select_failover_node_same_subscription(session, tenant_id=tenant_id, from_node=from_node)
    if node is None:
        raise ValueError("同订阅内无可用健康节点，无法故障切换")
    return node


def _select_failover_node_same_subscription(
    session: Session,
    *,
    tenant_id: int,
    from_node: ProxyAirportNode,
) -> ProxyAirportNode | None:
    """同订阅内选择下一个健康且独占可用的节点。"""
    stmt = (
        select(ProxyAirportNode)
        .join(ProxyAirportSubscription, ProxyAirportSubscription.id == ProxyAirportNode.subscription_id)
        .where(
            ProxyAirportNode.tenant_id == tenant_id,
            ProxyAirportNode.subscription_id == from_node.subscription_id,
            ProxyAirportNode.id != from_node.id,
            ProxyAirportNode.status == "healthy",
            ProxyAirportSubscription.enabled.is_(True),
            ProxyAirportSubscription.sync_status == "synced",
        )
        .order_by(ProxyAirportNode.id)
    )
    for node in session.scalars(stmt).all():
        if _is_node_exclusively_available(session, tenant_id=tenant_id, node=node):
            return node
    return None


def _is_node_exclusively_available(
    session: Session,
    *,
    tenant_id: int,
    node: ProxyAirportNode,
) -> bool:
    """节点未被授权槽位级绑定且未被其他降权分组绑定。"""
    slot_binding = session.scalar(
        select(AccountProxyBinding.id).where(
            AccountProxyBinding.tenant_id == tenant_id,
            AccountProxyBinding.proxy_airport_node_id == node.id,
            AccountProxyBinding.status == "active",
            AccountProxyBinding.unbound_at.is_(None),
        ).limit(1)
    )
    if slot_binding is not None:
        return False
    group_binding = session.scalar(
        select(AccountGroupProxyBinding.id).where(
            AccountGroupProxyBinding.tenant_id == tenant_id,
            AccountGroupProxyBinding.proxy_airport_node_id == node.id,
            AccountGroupProxyBinding.status == "active",
            AccountGroupProxyBinding.unbound_at.is_(None),
        ).limit(1)
    )
    return group_binding is None


def verify_group_proxy_egress(
    session: Session,
    *,
    binding_id: int,
    probe_exit_ip: str | None = None,
) -> bool:
    """分组级代理出口探测（简化版）。

    本任务实现简化版：调用方（Task 13 Executor）通过代理探测得到出口 IP 后传入
    ``probe_exit_ip``，本函数与 binding.observed_exit_ip 比对。
    真实 Telegram 连接探测留给 Task 13 Executor。

    Returns:
        True: 出口 IP 一致或首次探测成功；False: 探测失败或出口 IP 漂移。
    """
    binding = session.get(AccountGroupProxyBinding, int(binding_id))
    if binding is None or binding.status != "active":
        return False

    if probe_exit_ip is None or not probe_exit_ip.strip():
        return False

    probed = probe_exit_ip.strip()
    observed = (binding.observed_exit_ip or "").strip()
    if observed and observed != probed:
        return False

    now = _now()
    if not observed:
        binding.observed_exit_ip = probed
    binding.last_health_check_at = now
    session.flush()
    return True


__all__ = [
    "binding_snapshot",
    "create_group_proxy_binding",
    "create_or_update_rank_deboost_proxy_binding",
    "delete_rank_deboost_proxy_binding",
    "failover_group_proxy_binding",
    "get_active_group_binding",
    "rank_deboost_pool_reference_count",
    "unbind_group_proxy_binding",
    "verify_group_proxy_egress",
]
