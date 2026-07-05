from __future__ import annotations

import base64
import binascii
import json
import re
import socket
import urllib.parse
import urllib.request
from typing import Any, Callable
from urllib.parse import urlsplit

import yaml
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ProxyAirportNode, ProxyAirportSubscription
from app.schemas.account_environment import ProxyAirportSubscriptionOut, ProxyAirportSubscriptionUpdate
from app.security import decrypt_secret, encrypt_secret
from app.services._common import _now, audit

MAX_SUBSCRIPTION_BYTES = 5 * 1024 * 1024
NODE_HEALTH_TIMEOUT_SECONDS = 3
NODE_SKIP_RE = re.compile(r"(剩余|套餐|到期|官网|流量|expire|traffic)", re.IGNORECASE)
SubscriptionFetcher = Callable[[str], str]
NodeHealthChecker = Callable[[ProxyAirportNode], tuple[bool, str]]


def mask_subscription_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    path = parsed.path or "/"
    suffix = url[-4:] if len(url) >= 4 else url
    return f"{parsed.scheme}://{parsed.netloc}{path}?...{suffix}"


def get_proxy_airport_subscription(session: Session, *, tenant_id: int) -> ProxyAirportSubscriptionOut:
    row = _active_subscription(session, tenant_id)
    if row is None:
        return _empty_subscription(tenant_id)
    return _subscription_out(row)


def update_proxy_airport_subscription(
    session: Session,
    *,
    tenant_id: int,
    payload: ProxyAirportSubscriptionUpdate,
    actor: str,
) -> ProxyAirportSubscriptionOut:
    row = _active_subscription(session, tenant_id)
    if row is None:
        row = ProxyAirportSubscription(tenant_id=tenant_id, is_active=True)
        session.add(row)
    row.subscription_url_ciphertext = encrypt_secret(payload.subscription_url)
    row.subscription_url_preview = mask_subscription_url(payload.subscription_url)
    row.sync_status = "configured"
    row.node_count = 0
    row.healthy_node_count = 0
    row.last_sync_at = None
    row.last_error = ""
    row.updated_at = _now()
    if row.id is not None:
        _delete_subscription_nodes(session, row)
    session.flush()
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="保存全局 Clash 订阅",
        target_type="proxy_airport_subscription",
        target_id=str(row.id),
        detail=f"preview={row.subscription_url_preview}",
    )
    return _subscription_out(row)


def mark_proxy_airport_subscription_tested(session: Session, *, tenant_id: int, actor: str) -> ProxyAirportSubscriptionOut:
    return sync_proxy_airport_subscription(session, tenant_id=tenant_id, actor=actor, health_checker=check_proxy_airport_node)


def sync_proxy_airport_subscription(
    session: Session,
    *,
    tenant_id: int,
    actor: str,
    fetcher: SubscriptionFetcher | None = None,
    health_checker: NodeHealthChecker | None = None,
) -> ProxyAirportSubscriptionOut:
    row = _active_subscription(session, tenant_id)
    if row is None or not row.subscription_url_ciphertext:
        raise ValueError("clash_subscription_not_configured")
    try:
        raw = (fetcher or fetch_subscription)(_decrypt_subscription_url(row))
        nodes = parsed_proxy_nodes(raw)
        _replace_subscription_nodes(session, row, nodes)
        session.flush()
        node_rows = _subscription_nodes(session, row)
        if health_checker is not None:
            _apply_node_health_checks(node_rows, health_checker)
        row.sync_status = "synced"
        row.node_count = len(node_rows)
        row.healthy_node_count = _healthy_node_count(node_rows)
        row.last_error = ""
        row.last_sync_at = _now()
        row.updated_at = _now()
        session.flush()
    except ValueError as exc:
        _record_sync_failure(session, row, actor, str(exc))
        raise
    except OSError as exc:
        _record_sync_failure(session, row, actor, "proxy_airport_subscription_fetch_failed")
        raise ValueError("proxy_airport_subscription_fetch_failed") from exc
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="同步全局 Clash 订阅",
        target_type="proxy_airport_subscription",
        target_id=str(row.id),
        detail=f"nodes={row.node_count}; healthy={row.healthy_node_count}",
    )
    return _subscription_out(row)


def check_proxy_airport_node(node: ProxyAirportNode) -> tuple[bool, str]:
    try:
        with socket.create_connection((node.proxy_host, node.proxy_port), timeout=NODE_HEALTH_TIMEOUT_SECONDS):
            return True, ""
    except OSError as exc:
        return False, exc.__class__.__name__


def fetch_subscription(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "tg-yunying-clash-sync/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(MAX_SUBSCRIPTION_BYTES + 1)
    if len(raw) > MAX_SUBSCRIPTION_BYTES:
        raise ValueError("subscription_response_too_large")
    return raw.decode("utf-8", errors="replace")


def parsed_proxy_nodes(raw: str) -> list[dict[str, Any]]:
    text = _subscription_text(raw)
    configs = _structured_proxy_configs(text) if _looks_structured(text) else _uri_proxy_configs(text)
    nodes = [_node_record(index, item) for index, item in enumerate(configs, start=1)]
    if not nodes:
        raise ValueError("no_supported_proxy_nodes")
    return nodes


def _structured_proxy_configs(text: str) -> list[dict[str, Any]]:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError("invalid_clash_subscription_yaml") from exc
    items = loaded if isinstance(loaded, list) else loaded.get("proxies") if isinstance(loaded, dict) else None
    if not isinstance(items, list):
        raise ValueError("clash_subscription_missing_proxies")
    return [_normalized_structured_config(item) for item in items if _usable_structured_config(item)]


def _uri_proxy_configs(text: str) -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    for line in [item.strip() for item in text.splitlines()]:
        if not line or "://" not in line or NODE_SKIP_RE.search(line):
            continue
        config = _parse_proxy_uri(line, len(configs) + 1)
        if config is not None:
            configs.append(config)
    if not configs:
        raise ValueError("no_supported_proxy_nodes")
    return configs


def _parse_proxy_uri(uri: str, index: int) -> dict[str, Any] | None:
    scheme = uri.split(":", 1)[0].lower()
    if scheme in {"trojan", "anytls", "vless"}:
        return _base_uri_config(uri, scheme, index)
    if scheme == "vmess":
        return _vmess_uri_config(uri, index)
    if scheme == "ss":
        return _shadowsocks_uri_config(uri, index)
    return None


def _base_uri_config(uri: str, protocol: str, index: int) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(uri)
    port = int(parsed.port or 0)
    if not parsed.hostname or not port:
        raise ValueError("invalid_proxy_node")
    query = urllib.parse.parse_qs(parsed.query)
    config = {
        "name": _sanitize_name(urllib.parse.unquote(parsed.fragment or f"{protocol}-{index:03d}")),
        "type": protocol,
        "server": parsed.hostname,
        "port": port,
    }
    credential_field = "uuid" if protocol == "vless" else "password"
    config[credential_field] = urllib.parse.unquote(parsed.username or "")
    if _first(query, "sni"):
        config["sni"] = _first(query, "sni")
    if _first(query, "type"):
        config["network"] = _first(query, "type")
    return config


def _first(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name) or []
    return urllib.parse.unquote(values[0]) if values else ""


def _node_label(protocol: str, index: int) -> str:
    return f"{protocol}-{index:03d}"


def _validate_host_port(config: dict[str, Any]) -> dict[str, Any]:
    host = str(config.get("server") or config.get("host") or "").strip()
    port = int(config.get("port") or 0)
    if not host or port <= 0:
        raise ValueError("invalid_proxy_node")
    config["server"] = host
    config["port"] = port
    return config


def _config_from_structured(item: Any) -> dict[str, Any]:
    config = dict(item)
    config["name"] = _sanitize_name(config["name"])
    config["type"] = str(config["type"]).strip().lower()
    return _validate_host_port(config)


def _vmess_uri_config(uri: str, index: int) -> dict[str, Any] | None:
    data = json.loads(_decode_base64_text(uri.split("://", 1)[1]))
    config = {
        "name": _sanitize_name(data.get("ps") or _node_label("vmess", index)),
        "type": "vmess",
        "server": data.get("add"),
        "port": int(data.get("port") or 0),
        "uuid": data.get("id"),
        "alterId": int(data.get("aid") or 0),
        "cipher": "auto",
    }
    return _validate_host_port(config) if config["uuid"] else None


def _shadowsocks_uri_config(uri: str, index: int) -> dict[str, Any] | None:
    parsed = urllib.parse.urlsplit(uri)
    userinfo = urllib.parse.unquote(parsed.username or "")
    if ":" not in userinfo:
        userinfo = _decode_base64_text(userinfo)
    if ":" not in userinfo:
        return None
    cipher, password = userinfo.split(":", 1)
    return _validate_host_port({
        "name": _sanitize_name(urllib.parse.unquote(parsed.fragment or _node_label("ss", index))),
        "type": "ss",
        "server": parsed.hostname or "",
        "port": int(parsed.port or 0),
        "cipher": cipher,
        "password": password,
    })


def _replace_subscription_nodes(session: Session, row: ProxyAirportSubscription, nodes: list[dict[str, Any]]) -> None:
    _delete_subscription_nodes(session, row)
    for node in nodes:
        session.add(
            ProxyAirportNode(
                tenant_id=row.tenant_id,
                subscription_id=row.id,
                node_key=node["node_key"],
                node_name=node["node_name"],
                protocol=node["protocol"],
                proxy_host=node["proxy_host"],
                proxy_port=int(node["proxy_port"]),
                node_config_ciphertext=node["node_config_ciphertext"],
                status="unknown",
            )
        )


def _subscription_nodes(session: Session, row: ProxyAirportSubscription) -> list[ProxyAirportNode]:
    stmt = select(ProxyAirportNode).where(
        ProxyAirportNode.tenant_id == row.tenant_id,
        ProxyAirportNode.subscription_id == row.id,
    )
    return list(session.scalars(stmt.order_by(ProxyAirportNode.node_key)).all())


def _apply_node_health_checks(nodes: list[ProxyAirportNode], health_checker: NodeHealthChecker) -> None:
    for node in nodes:
        healthy, error = health_checker(node)
        node.status = "healthy" if healthy else "unhealthy"
        node.last_error = "" if healthy else str(error or "proxy_airport_node_unhealthy")[:200]
        node.updated_at = _now()


def _healthy_node_count(nodes: list[ProxyAirportNode]) -> int:
    return sum(1 for node in nodes if node.status == "healthy")


def _record_sync_failure(session: Session, row: ProxyAirportSubscription, actor: str, error: str) -> None:
    row.sync_status = "failed"
    row.node_count = 0
    row.healthy_node_count = 0
    row.last_error = error
    row.last_sync_at = _now()
    row.updated_at = _now()
    _delete_subscription_nodes(session, row)
    session.flush()
    audit(session, tenant_id=row.tenant_id, actor=actor, action="同步全局 Clash 订阅失败", target_type="proxy_airport_subscription", target_id=str(row.id), detail=error)


def _delete_subscription_nodes(session: Session, row: ProxyAirportSubscription) -> None:
    session.execute(delete(ProxyAirportNode).where(ProxyAirportNode.tenant_id == row.tenant_id, ProxyAirportNode.subscription_id == row.id))


def _active_subscription(session: Session, tenant_id: int) -> ProxyAirportSubscription | None:
    stmt = select(ProxyAirportSubscription).where(
        ProxyAirportSubscription.tenant_id == tenant_id,
        ProxyAirportSubscription.enabled.is_(True),
    )
    return session.scalar(stmt.order_by(ProxyAirportSubscription.priority, ProxyAirportSubscription.id).limit(1))


def _empty_subscription(tenant_id: int) -> ProxyAirportSubscriptionOut:
    return ProxyAirportSubscriptionOut(
        id=None,
        tenant_id=tenant_id,
        subscription_url_configured=False,
        subscription_url_preview="",
        sync_status="not_configured",
        node_count=0,
        healthy_node_count=0,
    )


def _subscription_out(row: ProxyAirportSubscription) -> ProxyAirportSubscriptionOut:
    return ProxyAirportSubscriptionOut(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        subscription_url_configured=bool(row.subscription_url_ciphertext),
        subscription_url_preview=row.subscription_url_preview,
        provider_type=row.provider_type,
        priority=row.priority,
        enabled=row.enabled,
        failover_policy=row.failover_policy,
        auto_failback_enabled=row.auto_failback_enabled,
        failback_cooldown_minutes=row.failback_cooldown_minutes,
        all_subscriptions_down_policy=row.all_subscriptions_down_policy,
        notify_admin_on_all_subscriptions_down=row.notify_admin_on_all_subscriptions_down,
        sync_status=row.sync_status,
        node_count=row.node_count,
        healthy_node_count=row.healthy_node_count,
        last_sync_at=row.last_sync_at,
        last_error=row.last_error,
        updated_at=row.updated_at,
    )


def _decrypt_subscription_url(row: ProxyAirportSubscription) -> str:
    url = decrypt_secret(row.subscription_url_ciphertext) or ""
    if not url:
        raise ValueError("clash_subscription_not_configured")
    return url


def _subscription_text(raw: str) -> str:
    if "://" in raw or _looks_structured(raw):
        return raw
    decoded = _decode_base64_text(raw)
    return decoded if decoded.strip() else raw


def _decode_base64_text(raw: str) -> str:
    compact = "".join(raw.strip().split())
    padded = compact + "=" * (-len(compact) % 4)
    try:
        return base64.b64decode(padded, validate=False).decode("utf-8", errors="replace")
    except binascii.Error:
        return ""


def _looks_structured(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith(("{", "[")) or "proxies:" in text


def _usable_structured_config(item: Any) -> bool:
    return isinstance(item, dict) and bool(item.get("name")) and bool(item.get("type")) and not NODE_SKIP_RE.search(str(item.get("name") or ""))


def _normalized_structured_config(item: Any) -> dict[str, Any]:
    return _config_from_structured(item)


def _node_record(index: int, config: dict[str, Any]) -> dict[str, str]:
    return {
        "node_key": f"{index:03d}-{_sanitize_key(config['name'])}",
        "node_name": _sanitize_name(config["name"]),
        "protocol": str(config["type"]).strip().lower()[:40],
        "proxy_host": str(config["server"]).strip(),
        "proxy_port": str(int(config["port"])),
        "node_config_ciphertext": encrypt_secret(json.dumps(config, ensure_ascii=False, sort_keys=True)),
    }


def _sanitize_name(raw: Any) -> str:
    name = str(raw or "").strip()[:120]
    return name or "proxy-node"


def _sanitize_key(raw: Any) -> str:
    key = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(raw or "").strip().lower()).strip("-")
    return (key or "proxy-node")[:120]


__all__ = [
    "create_proxy_airport_subscription",
    "fetch_subscription",
    "failover_proxy_airport_node_binding",
    "get_proxy_airport_subscription",
    "check_proxy_airport_node",
    "list_proxy_airport_subscriptions",
    "mark_proxy_airport_subscription_tested",
    "mask_subscription_url",
    "patch_proxy_airport_subscription",
    "parsed_proxy_nodes",
    "select_proxy_airport_subscription_for_failover",
    "sync_proxy_airport_subscription",
    "sync_proxy_airport_subscription_by_id",
    "update_proxy_airport_subscription",
]


from .proxy_airport_pool import (  # noqa: E402
    create_proxy_airport_subscription,
    list_proxy_airport_subscriptions,
    patch_proxy_airport_subscription,
    select_proxy_airport_subscription_for_failover,
    sync_proxy_airport_subscription_by_id,
)
from .proxy_airport_failover import failover_proxy_airport_node_binding  # noqa: E402
