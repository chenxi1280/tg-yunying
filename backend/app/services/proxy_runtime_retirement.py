from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AccountEnvironmentBinding,
    AccountGroupProxyBinding,
    AccountProxy,
    AccountProxyBinding,
    TgAccount,
    TgAccountAuthorization,
    TgAccountOnlineState,
    TgLoginFlow,
)
from app.services._common import _now, audit


LOGIN_TERMINAL_STATUSES = frozenset(
    {"active", "error", "已取消", "已过期", "superseded"}
)


@dataclass(frozen=True)
class ProxyConsumerCounts:
    accounts: int
    authorizations: int
    active_proxy_bindings: int
    active_environments: int
    active_group_bindings: int
    desired_online_states: int
    open_login_flows: int

    def has_consumers(self) -> bool:
        return any(asdict(self).values())


@dataclass(frozen=True)
class ProxyRuntimeRecord:
    id: int
    tenant_id: int
    name: str
    status: str
    alert_status: str
    disabled_reason: str
    updated_at: str
    consumers: ProxyConsumerCounts

    def payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "status": self.status,
            "alert_status": self.alert_status,
            "disabled_reason": self.disabled_reason,
            "updated_at": self.updated_at,
            "consumers": asdict(self.consumers),
        }

    def state_hash(self) -> str:
        encoded = json.dumps(
            self.payload(), ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class ProxyRetirementRequest:
    expected_state_hashes: dict[str, str]
    actor: str
    approval_ref: str


def snapshot_proxy_runtimes(
    session: Session,
    names: tuple[str, ...],
    *,
    lock: bool = False,
) -> tuple[ProxyRuntimeRecord, ...]:
    statement = select(AccountProxy).where(AccountProxy.name.in_(names)).order_by(
        AccountProxy.name
    )
    if lock:
        statement = statement.with_for_update()
    proxies = tuple(session.scalars(statement))
    found = {proxy.name for proxy in proxies}
    missing = sorted(set(names) - found)
    if missing:
        raise RuntimeError(f"proxy_runtime_target_missing:{','.join(missing)}")
    return tuple(_runtime_record(session, proxy) for proxy in proxies)


def retire_proxy_runtimes(
    session: Session,
    request: ProxyRetirementRequest,
) -> tuple[ProxyRuntimeRecord, ...]:
    names = tuple(sorted(request.expected_state_hashes))
    _validate_request(request, names)
    records = snapshot_proxy_runtimes(session, names, lock=True)
    for record in records:
        _validate_record(record, request.expected_state_hashes[record.name])
    now = _now()
    for record in records:
        _retire_record(session, record, request, now=now)
    session.flush()
    return records


def _runtime_record(session: Session, proxy: AccountProxy) -> ProxyRuntimeRecord:
    return ProxyRuntimeRecord(
        id=proxy.id,
        tenant_id=proxy.tenant_id,
        name=proxy.name,
        status=proxy.status,
        alert_status=proxy.alert_status,
        disabled_reason=proxy.disabled_reason,
        updated_at=_timestamp(proxy.updated_at),
        consumers=_consumer_counts(session, proxy.id),
    )


def _consumer_counts(session: Session, proxy_id: int) -> ProxyConsumerCounts:
    binding_ids = select(AccountProxyBinding.id).where(
        AccountProxyBinding.proxy_id == proxy_id
    )
    return ProxyConsumerCounts(
        accounts=_count(
            session, TgAccount.id, conditions=(TgAccount.proxy_id == proxy_id,)
        ),
        authorizations=_count(
            session,
            TgAccountAuthorization.id,
            conditions=(TgAccountAuthorization.proxy_id == proxy_id,),
        ),
        active_proxy_bindings=_count_active_proxy_bindings(session, proxy_id),
        active_environments=_count_active_environments(session, proxy_id, binding_ids),
        active_group_bindings=_count_active_group_bindings(session, proxy_id),
        desired_online_states=_count(
            session,
            TgAccountOnlineState.id,
            conditions=(
                TgAccountOnlineState.proxy_id == proxy_id,
                TgAccountOnlineState.desired_online.is_(True),
            ),
        ),
        open_login_flows=_count(
            session,
            TgLoginFlow.id,
            conditions=(
                TgLoginFlow.proxy_id == proxy_id,
                TgLoginFlow.status.not_in(LOGIN_TERMINAL_STATUSES),
            ),
        ),
    )


def _count(session: Session, column, *, conditions: tuple) -> int:
    return int(session.scalar(select(func.count(column)).where(*conditions)) or 0)


def _count_active_proxy_bindings(session: Session, proxy_id: int) -> int:
    return _count(
        session,
        AccountProxyBinding.id,
        conditions=(
            AccountProxyBinding.proxy_id == proxy_id,
            AccountProxyBinding.status == "active",
            AccountProxyBinding.unbound_at.is_(None),
        ),
    )


def _count_active_environments(session: Session, proxy_id: int, binding_ids) -> int:
    return _count(
        session,
        AccountEnvironmentBinding.id,
        conditions=(
            or_(
                AccountEnvironmentBinding.proxy_id == proxy_id,
                AccountEnvironmentBinding.proxy_binding_id.in_(binding_ids),
            ),
            AccountEnvironmentBinding.status == "active",
            AccountEnvironmentBinding.unbound_at.is_(None),
        ),
    )


def _count_active_group_bindings(session: Session, proxy_id: int) -> int:
    return _count(
        session,
        AccountGroupProxyBinding.id,
        conditions=(
            AccountGroupProxyBinding.runtime_proxy_id == proxy_id,
            AccountGroupProxyBinding.status == "active",
            AccountGroupProxyBinding.unbound_at.is_(None),
        ),
    )


def _validate_request(request: ProxyRetirementRequest, names: tuple[str, ...]) -> None:
    if not names:
        raise ValueError("proxy_runtime_retirement_targets_required")
    if not request.actor.strip():
        raise ValueError("proxy_runtime_retirement_actor_required")
    if not request.approval_ref.strip():
        raise ValueError("proxy_runtime_retirement_approval_ref_required")


def _validate_record(record: ProxyRuntimeRecord, expected_hash: str) -> None:
    if record.state_hash() != expected_hash:
        raise RuntimeError(f"proxy_runtime_state_hash_mismatch:{record.name}")
    if record.consumers.has_consumers():
        raise RuntimeError(f"proxy_runtime_has_consumers:{record.name}")


def _retire_record(
    session: Session,
    record: ProxyRuntimeRecord,
    request: ProxyRetirementRequest,
    *,
    now: datetime,
) -> None:
    proxy = session.get(AccountProxy, record.id)
    if proxy is None or proxy.name != record.name:
        raise RuntimeError(f"proxy_runtime_target_drift:{record.name}")
    reason = f"unused_mihomo_runtime_retired; approval_ref={request.approval_ref}"
    proxy.status = "disabled"
    proxy.alert_status = "disabled"
    proxy.disabled_reason = reason
    proxy.updated_at = now
    audit(
        session,
        tenant_id=record.tenant_id,
        actor=request.actor,
        action="退役零消费者代理运行时",
        target_type="account_proxy",
        target_id=str(record.id),
        detail=f"proxy={record.name}; approval_ref={request.approval_ref}; old_state_hash={record.state_hash()}",
    )


def _timestamp(value: datetime | None) -> str:
    return value.isoformat() if value else ""


__all__ = [
    "ProxyRetirementRequest",
    "ProxyRuntimeRecord",
    "retire_proxy_runtimes",
    "snapshot_proxy_runtimes",
]
