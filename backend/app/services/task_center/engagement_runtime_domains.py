from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountPoolConcurrencyLease,
    ExecutionResiliencePolicyRevision,
    TgAccount,
    TgAccountAuthorization,
)
from app.models.risk_control import AccountProxyBinding


ACTIVE_DOMAIN_LEASE_STATES = ("reserved", "call_issued", "remote_unknown")


def proxy_domain_keys(session: Session, account: TgAccount) -> tuple[str, str]:
    authorization = (
        session.get(TgAccountAuthorization, account.current_authorization_id)
        if account.current_authorization_id
        else None
    )
    proxy_id = int(
        (authorization.proxy_id if authorization else None)
        or account.proxy_id
        or 0
    )
    if proxy_id <= 0:
        return "", ""
    binding = _latest_proxy_binding(session, account, proxy_id)
    route_key = f"proxy:{proxy_id}"
    exit_ip = str(binding.observed_exit_ip or "").strip() if binding else ""
    egress_key = f"exit:{exit_ip}" if exit_ip else route_key
    return route_key, egress_key


def proxy_capacity_blocker(
    session: Session,
    *,
    tenant_id: int,
    policy: ExecutionResiliencePolicyRevision,
    route_key: str,
    egress_key: str,
) -> tuple[str, str] | None:
    if not route_key:
        return None
    route_count = _lease_count(
        session,
        tenant_id,
        column_name="proxy_route_key",
        key=route_key,
    )
    if route_count >= int(policy.proxy_route_inflight_limit):
        return "proxy_route_inflight_full", "代理路由并发已满"
    egress_count = _lease_count(
        session,
        tenant_id,
        column_name="proxy_egress_key",
        key=egress_key,
    )
    if egress_count >= int(policy.proxy_egress_inflight_limit):
        return "proxy_egress_inflight_full", "代理出口并发已满"
    return None


def new_pool_lease(
    action,
    *,
    attempt,
    account,
    binding,
    policy,
    pool_id: int,
    route_key: str,
    egress_key: str,
) -> AccountPoolConcurrencyLease:
    return AccountPoolConcurrencyLease(
        tenant_id=action.tenant_id,
        policy_revision_id=policy.id,
        account_pool_id=pool_id,
        task_id=action.task_id,
        account_id=account.id,
        action_id=action.id,
        attempt_id=attempt.id,
        invocation_identity=attempt.id,
        task_group_share_limit=binding.concurrency_limit_per_group,
        proxy_route_key=route_key,
        proxy_egress_key=egress_key,
    )


def _latest_proxy_binding(
    session: Session,
    account: TgAccount,
    proxy_id: int,
) -> AccountProxyBinding | None:
    return session.scalar(
        select(AccountProxyBinding)
        .where(
            AccountProxyBinding.tenant_id == account.tenant_id,
            AccountProxyBinding.account_id == account.id,
            AccountProxyBinding.proxy_id == proxy_id,
            AccountProxyBinding.status == "active",
            AccountProxyBinding.unbound_at.is_(None),
        )
        .order_by(AccountProxyBinding.bound_at.desc(), AccountProxyBinding.id.desc())
        .limit(1)
    )


def _lease_count(
    session: Session,
    tenant_id: int,
    *,
    column_name: str,
    key: str,
) -> int:
    column = getattr(AccountPoolConcurrencyLease, column_name)
    query = select(func.count(AccountPoolConcurrencyLease.id)).where(
        AccountPoolConcurrencyLease.tenant_id == tenant_id,
        column == key,
        AccountPoolConcurrencyLease.state.in_(ACTIVE_DOMAIN_LEASE_STATES),
    )
    return int(session.scalar(query) or 0)


__all__ = [
    "ACTIVE_DOMAIN_LEASE_STATES",
    "new_pool_lease",
    "proxy_capacity_blocker",
    "proxy_domain_keys",
]
