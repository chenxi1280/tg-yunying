from __future__ import annotations

import json
import os
import socket
from collections import Counter

from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    AccountProxy,
    AccountProxyBinding,
    Action,
    ExecutionAttempt,
    ProxyAirportNode,
    SearchClickOpportunityAssignment,
)


TASK_IDS = tuple(filter(None, (
    value.strip() for value in os.environ["SEARCH_PROXY_TASK_IDS"].split(",")
)))
SAMPLE_LIMIT = 100
TCP_TIMEOUT_SECONDS = 2


def _tcp_probe(host: str, port: int) -> dict:
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT_SECONDS):
            return {"reachable": True, "error": ""}
    except OSError as exc:
        return {
            "reachable": False,
            "error": f"{exc.__class__.__name__}:{str(exc)[:120]}",
        }


def main() -> None:
    with SessionLocal() as session:
        rows = session.execute(
            select(ExecutionAttempt, SearchClickOpportunityAssignment)
            .join(Action, Action.id == ExecutionAttempt.action_id)
            .join(
                SearchClickOpportunityAssignment,
                SearchClickOpportunityAssignment.action_id == Action.id,
            )
            .where(Action.task_id.in_(TASK_IDS))
            .order_by(ExecutionAttempt.created_at.desc())
            .limit(SAMPLE_LIMIT)
        ).all()
        route_counts = Counter(
            assignment.proxy_route_id for _attempt, assignment in rows
        )
        routes = [
            _route_row(session, route_id, count)
            for route_id, count in route_counts.most_common()
        ]
        failure_counts = Counter(
            (attempt.failure_type, str(attempt.failure_detail or ""))
            for attempt, _assignment in rows
        )
        payload = {
            "attempt_count": len(rows),
            "failure_counts": [
                {"type": key[0], "detail": key[1][:120], "count": count}
                for key, count in failure_counts.most_common()
            ],
            "route_count": len(routes),
            "routes": routes,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _route_row(session, route_id: str, count: int) -> dict:
    binding = session.get(AccountProxyBinding, int(route_id))
    proxy = session.get(AccountProxy, binding.proxy_id) if binding else None
    node = (
        session.get(ProxyAirportNode, binding.proxy_airport_node_id)
        if binding and binding.proxy_airport_node_id else None
    )
    probe = _tcp_probe(proxy.host, proxy.port) if proxy else {
        "reachable": False,
        "error": "proxy_missing",
    }
    return {
        "route_id": route_id,
        "attempt_count": count,
        "binding_status": binding.status if binding else None,
        "proxy_id": proxy.id if proxy else None,
        "proxy_name": proxy.name if proxy else None,
        "proxy_host": proxy.host if proxy else None,
        "proxy_port": proxy.port if proxy else None,
        "proxy_status": proxy.status if proxy else None,
        "proxy_last_check_at": (
            proxy.last_check_at.isoformat() if proxy and proxy.last_check_at else None
        ),
        "proxy_last_error": str(proxy.last_error or "")[:160] if proxy else "",
        "node_id": node.id if node else None,
        "node_name": node.node_name if node else None,
        "node_status": node.status if node else None,
        "node_last_error": str(node.last_error or "")[:160] if node else "",
        "tcp_probe": probe,
    }


if __name__ == "__main__":
    main()
