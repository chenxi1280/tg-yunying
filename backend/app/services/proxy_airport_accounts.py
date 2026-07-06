from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountProxy, ProxyAirportNode, ProxyAirportSubscription


AIRPORT_NODE_PROXY_PREFIX = "airport-node-"
AVAILABLE_NODE_STATUS = "healthy"
SYNCED_SUBSCRIPTION_STATUS = "synced"


def list_available_proxy_airport_nodes(session: Session, *, tenant_id: int) -> list[ProxyAirportNode]:
    stmt = _available_nodes_stmt(tenant_id).order_by(ProxyAirportSubscription.priority, ProxyAirportNode.id)
    return list(session.scalars(stmt).all())


def require_available_proxy_airport_node(session: Session, *, tenant_id: int, node_id: int) -> ProxyAirportNode:
    stmt = _available_nodes_stmt(tenant_id).where(ProxyAirportNode.id == node_id)
    node = session.scalar(stmt.limit(1))
    if node is None:
        raise ValueError("proxy_airport_node_not_available")
    return node


def proxy_for_airport_node(session: Session, node: ProxyAirportNode) -> AccountProxy:
    name = f"{AIRPORT_NODE_PROXY_PREFIX}{node.id}"
    proxy = session.scalar(
        select(AccountProxy).where(
            AccountProxy.tenant_id == node.tenant_id,
            AccountProxy.name == name,
        )
    )
    if proxy is None:
        proxy = AccountProxy(tenant_id=node.tenant_id, name=name, port=int(node.proxy_port or 0))
        session.add(proxy)
    proxy.protocol = node.protocol or "socks5"
    proxy.host = node.proxy_host
    proxy.port = int(node.proxy_port or 0)
    proxy.status = AVAILABLE_NODE_STATUS
    proxy.alert_status = "normal"
    proxy.last_error = ""
    proxy.notes = "airport_clash node"
    session.flush()
    return proxy


def _available_nodes_stmt(tenant_id: int):
    return (
        select(ProxyAirportNode)
        .join(ProxyAirportSubscription, ProxyAirportSubscription.id == ProxyAirportNode.subscription_id)
        .where(
            ProxyAirportNode.tenant_id == tenant_id,
            ProxyAirportSubscription.tenant_id == tenant_id,
            ProxyAirportNode.status == AVAILABLE_NODE_STATUS,
            ProxyAirportSubscription.enabled.is_(True),
            ProxyAirportSubscription.sync_status == SYNCED_SUBSCRIPTION_STATUS,
            ProxyAirportSubscription.healthy_node_count > 0,
        )
    )
