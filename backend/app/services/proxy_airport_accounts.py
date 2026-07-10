from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountProxy, ProxyAirportNode, ProxyAirportSubscription


AIRPORT_NODE_PROXY_PREFIX = "airport-node-"
AVAILABLE_NODE_STATUS = "healthy"
SYNCED_SUBSCRIPTION_STATUS = "synced"
EXECUTABLE_PROXY_PROTOCOLS = frozenset({"socks5", "socks4", "http", "https"})
EXECUTABLE_PROXY_STATUSES = frozenset({"healthy", "健康", "active"})


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
    proxy = _existing_named_proxy(session, node, name)
    if proxy is not None and _is_executable_proxy(proxy):
        return proxy
    protocol = _node_executable_protocol(node)
    if protocol is None:
        raise ValueError("proxy_airport_node has no executable runtime proxy")
    _assert_node_endpoint(node)
    if proxy is None:
        proxy = AccountProxy(tenant_id=node.tenant_id, name=name, port=int(node.proxy_port))
        session.add(proxy)
    proxy.protocol = protocol
    proxy.host = node.proxy_host
    proxy.port = int(node.proxy_port)
    proxy.status = AVAILABLE_NODE_STATUS
    proxy.alert_status = "normal"
    proxy.last_error = ""
    proxy.notes = "airport_clash node"
    session.flush()
    return proxy


def _existing_named_proxy(session: Session, node: ProxyAirportNode, name: str) -> AccountProxy | None:
    return session.scalar(
        select(AccountProxy).where(
            AccountProxy.tenant_id == node.tenant_id,
            AccountProxy.name == name,
        )
    )


def _is_executable_proxy(proxy: AccountProxy) -> bool:
    protocol = (proxy.protocol or "").strip().lower()
    status = (proxy.status or "").strip()
    return (
        protocol in EXECUTABLE_PROXY_PROTOCOLS
        and bool((proxy.host or "").strip())
        and int(proxy.port or 0) > 0
        and status in EXECUTABLE_PROXY_STATUSES
    )


def _node_executable_protocol(node: ProxyAirportNode) -> str | None:
    protocol = (node.protocol or "").strip().lower()
    if not protocol:
        protocol = "socks5"
    return protocol if protocol in EXECUTABLE_PROXY_PROTOCOLS else None


def _assert_node_endpoint(node: ProxyAirportNode) -> None:
    if not (node.proxy_host or "").strip() or int(node.proxy_port or 0) <= 0:
        raise ValueError("proxy_airport_node_missing_executable_endpoint")


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
