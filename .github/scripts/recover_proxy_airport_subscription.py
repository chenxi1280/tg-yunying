from __future__ import annotations

from collections import Counter
import hashlib
import json
import os

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import AccountProxyBinding, AuditLog, ProxyAirportNode, ProxyAirportSubscription
from app.security import decrypt_secret
from app.services._common import _now
from app.services.proxy_airport_subscription import (
    check_proxy_airport_node,
    fetch_subscription,
    parsed_proxy_nodes,
)


ACTOR = "codex-production-recovery"
RETIRED_ERROR = "not_present_in_latest_subscription"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _subscription(session, subscription_id: int) -> ProxyAirportSubscription:
    row = session.get(ProxyAirportSubscription, subscription_id)
    if row is None or row.tenant_id != 1:
        raise RuntimeError("proxy_airport_subscription_not_found")
    return row


def _parsed_nodes(row: ProxyAirportSubscription) -> list[dict]:
    url = decrypt_secret(row.subscription_url_ciphertext) or ""
    if not url:
        raise RuntimeError("clash_subscription_not_configured")
    return parsed_proxy_nodes(fetch_subscription(url))


def _health(nodes: list[dict]) -> dict[str, tuple[bool, str]]:
    result: dict[str, tuple[bool, str]] = {}
    for item in nodes:
        node = ProxyAirportNode(proxy_host=item["proxy_host"], proxy_port=int(item["proxy_port"]))
        result[item["node_key"]] = check_proxy_airport_node(node)
    return result


def _reference_count(session, node_ids: tuple[int, ...]) -> int:
    if not node_ids:
        return 0
    return int(
        session.scalar(
            select(func.count())
            .select_from(AccountProxyBinding)
            .where(AccountProxyBinding.proxy_airport_node_id.in_(node_ids))
        )
        or 0
    )


def _fingerprint_payload(row, existing, incoming, health) -> dict:
    return {
        "subscription_id": row.id,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "existing": [(node.id, node.node_key, node.status, node.last_error) for node in existing],
        "incoming": [
            (item["node_key"], item["protocol"], item["proxy_host"], int(item["proxy_port"]))
            for item in incoming
        ],
        "health": sorted((key, value[0], value[1]) for key, value in health.items()),
    }


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _summary(session, row, existing, incoming, health, fingerprint) -> dict:
    incoming_keys = {item["node_key"] for item in incoming}
    node_ids = tuple(node.id for node in existing)
    return {
        "subscription_id": row.id,
        "name": row.name,
        "before_status": row.sync_status,
        "before_reported_node_count": row.node_count,
        "existing_node_count": len(existing),
        "existing_status_counts": dict(sorted(Counter(node.status for node in existing).items())),
        "existing_reference_count": _reference_count(session, node_ids),
        "incoming_node_count": len(incoming),
        "incoming_protocol_counts": dict(sorted(Counter(item["protocol"] for item in incoming).items())),
        "incoming_healthy_count": sum(1 for value in health.values() if value[0]),
        "matching_node_count": sum(1 for node in existing if node.node_key in incoming_keys),
        "retiring_node_count": sum(1 for node in existing if node.node_key not in incoming_keys),
        "fingerprint": fingerprint,
    }


def _apply_node(node: ProxyAirportNode, item: dict, healthy: tuple[bool, str]) -> None:
    node.node_name = item["node_name"]
    node.protocol = item["protocol"]
    node.proxy_host = item["proxy_host"]
    node.proxy_port = int(item["proxy_port"])
    node.node_config_ciphertext = item["node_config_ciphertext"]
    node.status = "healthy" if healthy[0] else "unhealthy"
    node.last_error = "" if healthy[0] else str(healthy[1] or "proxy_airport_node_unhealthy")[:200]
    node.updated_at = _now()


def _reconcile(session, row, existing, incoming, health) -> tuple[int, int]:
    by_key = {node.node_key: node for node in existing}
    incoming_keys = {item["node_key"] for item in incoming}
    inserted = 0
    for item in incoming:
        node = by_key.get(item["node_key"])
        if node is None:
            node = ProxyAirportNode(tenant_id=row.tenant_id, subscription_id=row.id, node_key=item["node_key"])
            session.add(node)
            inserted += 1
        _apply_node(node, item, health[item["node_key"]])
    retired = 0
    for node in existing:
        if node.node_key in incoming_keys:
            continue
        node.status = "retired"
        node.last_error = RETIRED_ERROR
        node.updated_at = _now()
        retired += 1
    session.flush()
    return inserted, retired


def _apply_subscription(row, incoming, health) -> None:
    row.sync_status = "synced"
    row.node_count = len(incoming)
    row.healthy_node_count = sum(1 for value in health.values() if value[0])
    row.last_sync_at = _now()
    row.last_error = ""
    row.updated_at = _now()


def _audit(session, row, fingerprint: str, inserted: int, retired: int) -> None:
    session.add(
        AuditLog(
            tenant_id=row.tenant_id,
            actor=ACTOR,
            action="恢复 Clash 订阅源节点对账",
            target_type="proxy_airport_subscription",
            target_id=str(row.id),
            detail=f"fingerprint={fingerprint}; inserted={inserted}; retired={retired}",
        )
    )


def main() -> None:
    subscription_id = _env_int("PROXY_AIRPORT_SUBSCRIPTION_ID", 3)
    with SessionLocal() as session:
        row = _subscription(session, subscription_id)
        existing = list(
            session.scalars(
                select(ProxyAirportNode)
                .where(ProxyAirportNode.tenant_id == 1, ProxyAirportNode.subscription_id == row.id)
                .order_by(ProxyAirportNode.id)
            )
        )
        incoming = _parsed_nodes(row)
        health = _health(incoming)
        fingerprint = _fingerprint(_fingerprint_payload(row, existing, incoming, health))
        summary = _summary(session, row, existing, incoming, health, fingerprint)
        if not _env_bool("PROXY_AIRPORT_RECOVERY_APPLY"):
            print("PROXY_AIRPORT_RECOVERY_DRY_RUN=" + json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return
        expected = os.getenv("PROXY_AIRPORT_RECOVERY_EXPECTED_FINGERPRINT", "").strip()
        if not expected or expected != fingerprint:
            raise RuntimeError("proxy_airport_recovery_fingerprint_mismatch")
        inserted, retired = _reconcile(session, row, existing, incoming, health)
        _apply_subscription(row, incoming, health)
        _audit(session, row, fingerprint, inserted, retired)
        session.commit()
        result = {**summary, "inserted_node_count": inserted, "retired_node_count": retired}
        print("PROXY_AIRPORT_RECOVERY_APPLIED=" + json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
