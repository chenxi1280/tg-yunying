from __future__ import annotations

from collections import Counter
import json

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    AccountGroupProxyBinding,
    AccountProxyBinding,
    AuditLog,
    ProxyAirportNode,
    ProxyAirportSubscription,
    ProxyExitIpObservation,
    SearchRankDeboostActionStat,
)
from app.security import decrypt_secret
from app.services.proxy_airport_subscription import fetch_subscription, parsed_proxy_nodes


def _count_refs(session, node_ids: tuple[int, ...]) -> dict[str, int]:
    if not node_ids:
        return {}
    references = {
        "account_proxy_bindings": AccountProxyBinding.proxy_airport_node_id,
        "account_group_proxy_bindings": AccountGroupProxyBinding.proxy_airport_node_id,
        "proxy_exit_ip_observations": ProxyExitIpObservation.proxy_node_id,
        "search_rank_deboost_action_stats": SearchRankDeboostActionStat.proxy_airport_node_id,
    }
    return {
        name: int(
            session.scalar(
                select(func.count()).select_from(column.class_).where(column.in_(node_ids))
            )
            or 0
        )
        for name, column in references.items()
    }


def _subscription_row(session, row: ProxyAirportSubscription) -> dict:
    nodes = tuple(
        session.scalars(
            select(ProxyAirportNode.id).where(
                ProxyAirportNode.tenant_id == row.tenant_id,
                ProxyAirportNode.subscription_id == row.id,
            )
        )
    )
    return {
        "id": row.id,
        "name": row.name,
        "priority": row.priority,
        "enabled": row.enabled,
        "sync_status": row.sync_status,
        "reported_node_count": row.node_count,
        "reported_healthy_node_count": row.healthy_node_count,
        "actual_node_count": len(nodes),
        "node_reference_counts": _count_refs(session, nodes),
        "last_sync_at": row.last_sync_at.isoformat() if row.last_sync_at else None,
        "last_error": str(row.last_error or "")[:240],
    }


def _fetch_parse(row: ProxyAirportSubscription) -> dict:
    try:
        url = decrypt_secret(row.subscription_url_ciphertext) or ""
        raw = fetch_subscription(url)
        nodes = parsed_proxy_nodes(raw)
        return {
            "status": "parsed",
            "response_bytes": len(raw.encode()),
            "node_count": len(nodes),
            "protocol_counts": dict(sorted(Counter(item["protocol"] for item in nodes).items())),
        }
    except Exception as exc:
        return {
            "status": "error",
            "exception_type": exc.__class__.__name__,
            "error": str(exc)[:240],
        }


def _recent_audits(session) -> list[dict]:
    rows = session.scalars(
        select(AuditLog)
        .where(AuditLog.target_type == "proxy_airport_subscription")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(12)
    )
    return [
        {
            "created_at": row.created_at.isoformat(),
            "action": row.action,
            "target_id": row.target_id,
            "detail": str(row.detail or "")[:240],
        }
        for row in rows
    ]


def main() -> None:
    with SessionLocal() as session:
        subscriptions = list(
            session.scalars(
                select(ProxyAirportSubscription)
                .where(ProxyAirportSubscription.tenant_id == 1)
                .order_by(ProxyAirportSubscription.priority, ProxyAirportSubscription.id)
            )
        )
        unresolved = [row for row in subscriptions if row.sync_status != "synced"]
        payload = {
            "subscriptions": [_subscription_row(session, row) for row in subscriptions],
            "unresolved_fetch_parse": [
                {"id": row.id, "name": row.name, **_fetch_parse(row)} for row in unresolved
            ],
            "recent_audits": _recent_audits(session),
        }
    print("PROXY_AIRPORT_SYNC_DIAGNOSTICS=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
