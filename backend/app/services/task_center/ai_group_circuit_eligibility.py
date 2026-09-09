from __future__ import annotations

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models import ExecutionCircuitState, Task, TgAccount, TgAccountAuthorization
from app.models.risk_control import AccountProxyBinding


def blocked_coverage_account_ids(session: Session, task: Task) -> set[int]:
    if not _uses_circuit_aware_coverage(task):
        return set()
    domain_keys = _blocking_domain_keys(session, task.tenant_id)
    blocked = set(domain_keys.get("account", ()))
    route_keys = domain_keys.get("proxy_route", set())
    egress_keys = domain_keys.get("proxy_egress", set())
    if not route_keys and not egress_keys:
        return blocked
    for account_id, proxy_id, exit_ip in session.execute(_account_domain_rows(task)):
        route_key = f"proxy:{int(proxy_id or 0)}" if proxy_id else ""
        egress_key = f"exit:{str(exit_ip).strip()}" if exit_ip else route_key
        if route_key in route_keys or egress_key in egress_keys:
            blocked.add(int(account_id))
    return blocked


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


def _account_domain_rows(task: Task):
    proxy_id = func.coalesce(TgAccountAuthorization.proxy_id, TgAccount.proxy_id)
    binding_match = and_(
        AccountProxyBinding.tenant_id == TgAccount.tenant_id,
        AccountProxyBinding.account_id == TgAccount.id,
        AccountProxyBinding.proxy_id == proxy_id,
        AccountProxyBinding.status == "active",
        AccountProxyBinding.unbound_at.is_(None),
    )
    return (
        select(TgAccount.id, proxy_id, AccountProxyBinding.observed_exit_ip)
        .outerjoin(
            TgAccountAuthorization,
            and_(
                TgAccountAuthorization.id == TgAccount.current_authorization_id,
                TgAccountAuthorization.is_current.is_(True),
            ),
        )
        .outerjoin(AccountProxyBinding, binding_match)
        .where(TgAccount.tenant_id == task.tenant_id, TgAccount.deleted_at.is_(None))
    )


__all__ = ["blocked_coverage_account_ids"]
