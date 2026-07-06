from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountProxyBinding,
    AccountProxyWarmupState,
    AccountEnvironmentBinding,
    ProxyAirportNode,
    ProxyAirportSubscription,
    ProxyExitIpObservation,
    ProxyNodeFailoverEvent,
)
from app.services._common import _now
from app.services.proxy_airport_accounts import proxy_for_airport_node


def failover_proxy_airport_node_binding(
    session: Session,
    *,
    tenant_id: int,
    proxy_binding_id: int,
    reason: str,
    observed_error: str = "",
) -> AccountProxyBinding:
    binding = _active_airport_binding(session, tenant_id=tenant_id, proxy_binding_id=proxy_binding_id)
    from_node = session.get(ProxyAirportNode, int(binding.proxy_airport_node_id or 0))
    if from_node is None:
        raise ValueError("proxy_airport_node_binding_missing")
    to_node = _select_failover_node(session, tenant_id=tenant_id, from_node=from_node)
    if to_node is None:
        raise ValueError("airport_all_subscriptions_unavailable")
    now = _now()
    binding.status = "inactive"
    binding.unbound_at = now
    proxy = proxy_for_airport_node(session, to_node)
    new_binding = _new_binding(binding, to_node, proxy, reason, now)
    session.add(new_binding)
    session.flush()
    event = _failover_event(binding, new_binding, from_node, to_node, reason, observed_error)
    session.add(event)
    session.add(_warmup_state(new_binding, reason))
    _retarget_environment_binding(session, binding, new_binding)
    _record_exit_observation(session, new_binding, to_node)
    session.flush()
    new_binding.proxy_failover_event_id = event.id
    return new_binding


def _active_airport_binding(
    session: Session,
    *,
    tenant_id: int,
    proxy_binding_id: int,
) -> AccountProxyBinding:
    binding = session.get(AccountProxyBinding, proxy_binding_id)
    if binding is None or binding.tenant_id != tenant_id:
        raise ValueError("proxy_binding_not_found")
    if binding.status != "active" or binding.unbound_at is not None:
        raise ValueError("proxy_binding_inactive")
    if not binding.proxy_airport_node_id:
        raise ValueError("proxy_airport_node_binding_missing")
    return binding


def _select_failover_node(
    session: Session,
    *,
    tenant_id: int,
    from_node: ProxyAirportNode,
) -> ProxyAirportNode | None:
    same_subscription = _candidate_nodes(session, tenant_id=tenant_id, from_node=from_node, same_subscription=True)
    if same_subscription:
        return same_subscription[0]
    backup_nodes = _candidate_nodes(session, tenant_id=tenant_id, from_node=from_node, same_subscription=False)
    return backup_nodes[0] if backup_nodes else None


def _candidate_nodes(
    session: Session,
    *,
    tenant_id: int,
    from_node: ProxyAirportNode,
    same_subscription: bool,
) -> list[ProxyAirportNode]:
    stmt = (
        select(ProxyAirportNode)
        .join(ProxyAirportSubscription, ProxyAirportSubscription.id == ProxyAirportNode.subscription_id)
        .where(
            ProxyAirportNode.tenant_id == tenant_id,
            ProxyAirportNode.id != from_node.id,
            ProxyAirportNode.status == "healthy",
            ProxyAirportSubscription.enabled.is_(True),
            ProxyAirportSubscription.sync_status == "synced",
            ProxyAirportSubscription.healthy_node_count > 0,
        )
        .order_by(ProxyAirportSubscription.priority, ProxyAirportNode.id)
    )
    if same_subscription:
        stmt = stmt.where(ProxyAirportNode.subscription_id == from_node.subscription_id)
    else:
        stmt = stmt.where(ProxyAirportNode.subscription_id != from_node.subscription_id)
    return [node for node in session.scalars(stmt).all() if _has_available_capacity(session, node)]


def _has_available_capacity(session: Session, node: ProxyAirportNode) -> bool:
    stmt = select(AccountProxyBinding.id).where(
        AccountProxyBinding.proxy_airport_node_id == node.id,
        AccountProxyBinding.status == "active",
        AccountProxyBinding.unbound_at.is_(None),
    )
    return len(session.scalars(stmt).all()) < int(node.max_bound_accounts or 1)


def _new_binding(
    old: AccountProxyBinding,
    node: ProxyAirportNode,
    proxy: AccountProxy,
    reason: str,
    now,
) -> AccountProxyBinding:
    return AccountProxyBinding(
        tenant_id=old.tenant_id,
        account_id=old.account_id,
        developer_app_id=old.developer_app_id,
        developer_app_api_id_snapshot=old.developer_app_api_id_snapshot,
        authorization_id=old.authorization_id,
        session_role=old.session_role,
        proxy_id=proxy.id,
        proxy_airport_node_id=node.id,
        observed_exit_ip=node.observed_exit_ip,
        observed_exit_country=node.observed_exit_country,
        observed_exit_asn=node.observed_exit_asn,
        observed_exit_isp=node.observed_exit_isp,
        last_failover_at=now,
        binding_generation=int(old.binding_generation or 1) + 1,
        change_reason=reason,
        bound_by="proxy_airport_failover",
    )


def _failover_event(
    old: AccountProxyBinding,
    new: AccountProxyBinding,
    from_node: ProxyAirportNode,
    to_node: ProxyAirportNode,
    reason: str,
    observed_error: str,
) -> ProxyNodeFailoverEvent:
    return ProxyNodeFailoverEvent(
        tenant_id=old.tenant_id,
        account_id=old.account_id,
        developer_app_id=old.developer_app_id,
        authorization_id=old.authorization_id,
        session_role=old.session_role,
        from_subscription_id=from_node.subscription_id,
        to_subscription_id=to_node.subscription_id,
        from_node_id=from_node.id,
        to_node_id=to_node.id,
        reason=reason,
        outcome="switched",
        observed_error=observed_error,
    )


def _warmup_state(binding: AccountProxyBinding, reason: str) -> AccountProxyWarmupState:
    return AccountProxyWarmupState(
        tenant_id=binding.tenant_id,
        account_id=binding.account_id,
        developer_app_id=binding.developer_app_id,
        authorization_id=binding.authorization_id,
        session_role=binding.session_role,
        proxy_binding_id=binding.id,
        stage="pending_warmup",
        reset_reason=reason,
    )


def _retarget_environment_binding(
    session: Session,
    old: AccountProxyBinding,
    new: AccountProxyBinding,
) -> None:
    stmt = select(AccountEnvironmentBinding).where(
        AccountEnvironmentBinding.tenant_id == old.tenant_id,
        AccountEnvironmentBinding.account_id == old.account_id,
        AccountEnvironmentBinding.developer_app_id == old.developer_app_id,
        AccountEnvironmentBinding.authorization_id == old.authorization_id,
        AccountEnvironmentBinding.session_role == old.session_role,
        AccountEnvironmentBinding.proxy_binding_id == old.id,
        AccountEnvironmentBinding.status == "active",
        AccountEnvironmentBinding.unbound_at.is_(None),
    )
    binding = session.scalar(stmt.limit(1))
    if binding is None:
        return
    binding.proxy_binding_id = new.id
    binding.proxy_id = new.proxy_id
    binding.updated_at = _now()


def _record_exit_observation(
    session: Session,
    binding: AccountProxyBinding,
    node: ProxyAirportNode,
) -> None:
    if not node.observed_exit_ip:
        return
    session.add(
        ProxyExitIpObservation(
            tenant_id=binding.tenant_id,
            proxy_node_id=node.id,
            proxy_binding_id=binding.id,
            observed_exit_ip=node.observed_exit_ip,
            observed_exit_country=node.observed_exit_country,
            observed_exit_asn=node.observed_exit_asn,
            observed_exit_isp=node.observed_exit_isp,
            check_source="failover",
        )
    )
