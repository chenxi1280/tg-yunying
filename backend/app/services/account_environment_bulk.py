from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountEnvironmentBinding, AccountPool, AccountProxy, AccountProxyBinding, ProxyAirportNode, TgAccount
from app.schemas.account_environment import AccountEnvironmentProxyBatchBindOut, AccountEnvironmentProxyBatchBindRequest
from app.services._common import _now, audit
from app.services.proxy_airport_accounts import proxy_for_airport_node, require_available_proxy_airport_node


CODE_RECEIVER_POOL_KEY = "code_receiver"


def bind_account_environment_proxy_batch(
    session: Session,
    *,
    tenant_id: int,
    payload: AccountEnvironmentProxyBatchBindRequest,
    actor: str,
) -> AccountEnvironmentProxyBatchBindOut:
    pool = _require_operational_pool(session, tenant_id, payload.account_pool_id)
    proxy, airport_node = _resolve_proxy_source(session, tenant_id, payload)
    trace_id = uuid4().hex
    skipped: list[dict[str, int | str]] = []
    affected: list[int] = []
    for account in _pool_accounts(session, tenant_id, pool.id):
        bindings = _active_environment_bindings(session, tenant_id, account.id, payload.session_role)
        if not bindings:
            skipped.append({"account_id": account.id, "reason": "account_environment_binding_missing"})
            continue
        for binding in bindings:
            _apply_proxy(session, binding, proxy, airport_node, actor, payload.change_reason)
        affected.append(account.id)
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="按账号分组批量绑定授权环境代理",
        target_type="account_environment_binding",
        target_id=str(pool.id),
        detail=f"trace_id={trace_id}; pool={pool.name}; proxy_id={proxy.id}; proxy_airport_node_id={payload.proxy_airport_node_id or ''}; session_role={payload.session_role}; success={len(affected)}; failed={len(skipped)}; reason={payload.change_reason}",
    )
    session.flush()
    return AccountEnvironmentProxyBatchBindOut(
        success_count=len(affected),
        failed_count=len(skipped),
        skipped_accounts=skipped,
        affected_account_ids=affected,
        trace_id=trace_id,
    )


def _require_operational_pool(session: Session, tenant_id: int, pool_id: int) -> AccountPool:
    pool = session.get(AccountPool, pool_id)
    if pool is None or pool.tenant_id != tenant_id:
        raise ValueError("account_pool_not_found")
    if pool.pool_purpose == CODE_RECEIVER_POOL_KEY or pool.system_key == CODE_RECEIVER_POOL_KEY:
        raise ValueError("account_pool_not_operational")
    return pool


def _require_proxy(session: Session, tenant_id: int, proxy_id: int | None) -> AccountProxy | None:
    if proxy_id is None:
        return None
    proxy = session.get(AccountProxy, proxy_id)
    if proxy is None or proxy.tenant_id != tenant_id:
        raise ValueError("proxy_not_found")
    return proxy


def _resolve_proxy_source(
    session: Session,
    tenant_id: int,
    payload: AccountEnvironmentProxyBatchBindRequest,
) -> tuple[AccountProxy, ProxyAirportNode | None]:
    if payload.proxy_airport_node_id:
        node = require_available_proxy_airport_node(session, tenant_id=tenant_id, node_id=payload.proxy_airport_node_id)
        return proxy_for_airport_node(session, node), node
    proxy = _require_proxy(session, tenant_id, payload.proxy_id)
    if proxy is None:
        raise ValueError("proxy_not_found")
    return proxy, None


def _pool_accounts(session: Session, tenant_id: int, pool_id: int) -> list[TgAccount]:
    stmt = (
        select(TgAccount)
        .where(TgAccount.tenant_id == tenant_id, TgAccount.pool_id == pool_id, TgAccount.deleted_at.is_(None))
        .order_by(TgAccount.id.asc())
    )
    return list(session.scalars(stmt).all())


def _active_environment_bindings(
    session: Session,
    tenant_id: int,
    account_id: int,
    session_role: str,
) -> list[AccountEnvironmentBinding]:
    stmt = select(AccountEnvironmentBinding).where(
        AccountEnvironmentBinding.tenant_id == tenant_id,
        AccountEnvironmentBinding.account_id == account_id,
        AccountEnvironmentBinding.session_role == session_role,
        AccountEnvironmentBinding.status == "active",
        AccountEnvironmentBinding.unbound_at.is_(None),
    )
    return list(session.scalars(stmt.order_by(AccountEnvironmentBinding.updated_at.desc())).all())


def _apply_proxy(
    session: Session,
    binding: AccountEnvironmentBinding,
    proxy: AccountProxy,
    airport_node: ProxyAirportNode | None,
    actor: str,
    change_reason: str,
) -> None:
    _deactivate_proxy_bindings(_active_proxy_bindings(session, binding))
    binding.proxy_id = proxy.id
    binding.proxy_binding_id = _new_proxy_binding(session, binding, proxy, airport_node, actor, change_reason)
    binding.updated_at = _now()


def _active_proxy_bindings(session: Session, binding: AccountEnvironmentBinding) -> list[AccountProxyBinding]:
    stmt = select(AccountProxyBinding).where(
        AccountProxyBinding.tenant_id == binding.tenant_id,
        AccountProxyBinding.account_id == binding.account_id,
        AccountProxyBinding.developer_app_id == binding.developer_app_id,
        AccountProxyBinding.authorization_id == binding.authorization_id,
        AccountProxyBinding.session_role == binding.session_role,
        AccountProxyBinding.status == "active",
        AccountProxyBinding.unbound_at.is_(None),
    )
    return list(session.scalars(stmt).all())


def _deactivate_proxy_bindings(bindings: list[AccountProxyBinding]) -> None:
    now = _now()
    for binding in bindings:
        binding.status = "inactive"
        binding.unbound_at = now


def _new_proxy_binding(
    session: Session,
    binding: AccountEnvironmentBinding,
    proxy: AccountProxy,
    airport_node: ProxyAirportNode | None,
    actor: str,
    change_reason: str,
) -> int | None:
    proxy_binding = AccountProxyBinding(
        tenant_id=binding.tenant_id,
        account_id=binding.account_id,
        developer_app_id=binding.developer_app_id,
        developer_app_api_id_snapshot=binding.developer_app_api_id_snapshot,
        authorization_id=binding.authorization_id,
        session_role=binding.session_role,
        proxy_id=proxy.id,
        proxy_airport_node_id=airport_node.id if airport_node else None,
        observed_exit_ip=airport_node.observed_exit_ip if airport_node else "",
        observed_exit_country=airport_node.observed_exit_country if airport_node else "",
        observed_exit_asn=airport_node.observed_exit_asn if airport_node else "",
        observed_exit_isp=airport_node.observed_exit_isp if airport_node else "",
        change_reason=change_reason,
        bound_by=actor,
    )
    session.add(proxy_binding)
    session.flush()
    return proxy_binding.id
