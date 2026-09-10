from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ExecutionCircuitState, Task


def blocked_coverage_account_ids(session: Session, task: Task) -> set[int]:
    if not _uses_circuit_aware_coverage(task):
        return set()
    domain_keys = _blocking_domain_keys(session, task.tenant_id)
    return set(domain_keys.get("account", ()))


def _uses_circuit_aware_coverage(task: Task) -> bool:
    config = task.type_config if isinstance(task.type_config, dict) else {}
    return (
        task.type == "group_ai_chat"
        and task.fulfillment_contract_version == "fact_first_v3"
        and config.get("engagement_contract_version") == "unified_engagement_v1"
    )


def _blocking_domain_keys(session: Session, tenant_id: int) -> dict[str, set]:
    rows = session.execute(
        select(ExecutionCircuitState.domain_kind, ExecutionCircuitState.domain_key)
        .where(
            ExecutionCircuitState.tenant_id == tenant_id,
            ExecutionCircuitState.state != "closed",
        )
    )
    result: dict[str, set] = {}
    for kind, key in rows:
        value = _domain_value(str(kind), str(key))
        if value is not None:
            result.setdefault(str(kind), set()).add(value)
    return result


def _domain_value(kind: str, key: str):
    prefixes = {"account": "account:", "proxy_route": "proxy:", "proxy_egress": "exit:"}
    prefix = prefixes.get(kind)
    if not prefix or not key.startswith(prefix):
        return None
    value = key[len(prefix):].strip()
    if kind == "account":
        return int(value) if value.isdigit() else None
    return key


__all__ = ["blocked_coverage_account_ids"]
