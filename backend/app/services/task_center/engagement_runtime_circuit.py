from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountPoolConcurrencyLease,
    ExecutionCircuitState,
    ExecutionResiliencePolicyRevision,
    RemoteInvocationFence,
)
from app.services._common import _now
from app.timezone import as_beijing


PROXY_FAILURE_MARKERS = (
    "proxy",
    "代理",
    "connect",
    "connection",
    "timeout",
    "timed out",
    "unreachable",
    "network",
)
ACCOUNT_FAILURE_MARKERS = PROXY_FAILURE_MARKERS + (
    "session",
    "auth key",
    "auth_key",
    "unauthorized",
)


def circuit_blocker(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    route_key: str,
    egress_key: str,
) -> tuple[str, str] | None:
    for kind, key in _domain_keys(account_id, route_key, egress_key):
        state = _locked_state(session, tenant_id, kind=kind, key=key)
        if state is None or state.state == "closed":
            continue
        if state.state == "half_open":
            return "execution_circuit_half_open", f"{kind} 熔断探活调用仍在执行"
        if state.opened_until and as_beijing(state.opened_until) > as_beijing(_now()):
            return "execution_circuit_open", f"{kind} 熔断至 {state.opened_until.isoformat()}"
        return "execution_circuit_probe_pending", f"{kind} 熔断等待独立健康探测"
    return None


def record_unknown(
    session: Session,
    lease: AccountPoolConcurrencyLease,
    fence: RemoteInvocationFence,
    *,
    failure_code: str,
    failure_detail: str,
) -> None:
    domains = _unknown_failure_domains(
        lease,
        fence,
        failure_text=f"{failure_code} {failure_detail}".lower(),
    )
    if not domains:
        return
    policy = lock_resilience_policy(session, fence.resilience_policy_revision_id)
    _record_failure_domains(
        session,
        lease,
        policy,
        domains=domains,
        failure_code=failure_code,
    )


def _unknown_failure_domains(
    lease: AccountPoolConcurrencyLease,
    fence: RemoteInvocationFence,
    *,
    failure_text: str,
) -> tuple[tuple[str, str], ...]:
    all_domains = _domain_keys(
        lease.account_id, lease.proxy_route_key, lease.proxy_egress_key
    )
    if fence.transport_termination_state != "acknowledged":
        return all_domains
    if any(marker in failure_text for marker in PROXY_FAILURE_MARKERS):
        return all_domains
    if any(marker in failure_text for marker in ACCOUNT_FAILURE_MARKERS):
        return all_domains[:1]
    return ()


def record_confirmed(
    session: Session,
    lease: AccountPoolConcurrencyLease,
    fence: RemoteInvocationFence,
) -> None:
    lock_resilience_policy(session, fence.resilience_policy_revision_id)
    for kind, key in _domain_keys(
        lease.account_id, lease.proxy_route_key, lease.proxy_egress_key
    ):
        state = _locked_state(
            session,
            lease.tenant_id,
            kind=kind,
            key=key,
        )
        if state is None or state.state != "closed":
            continue
        state.failure_times = []
        state.last_failure_code = ""
        state.version = int(state.version or 0) + 1


def record_failed(
    session: Session,
    lease: AccountPoolConcurrencyLease,
    fence: RemoteInvocationFence,
    *,
    failure_type: str,
    failure_detail: str,
    remote_mutation_started: bool | None,
) -> None:
    text = f"{failure_type} {failure_detail}".lower()
    domains = _failure_domains(lease, text, remote_mutation_started)
    if not domains:
        return
    policy = lock_resilience_policy(session, fence.resilience_policy_revision_id)
    _record_failure_domains(
        session,
        lease,
        policy,
        domains=domains,
        failure_code=failure_type or "gateway_failure",
    )


def _failure_domains(
    lease: AccountPoolConcurrencyLease,
    text: str,
    remote_mutation_started: bool | None,
) -> tuple[tuple[str, str], ...]:
    all_domains = _domain_keys(
        lease.account_id, lease.proxy_route_key, lease.proxy_egress_key
    )
    if remote_mutation_started is None:
        return all_domains
    if any(marker in text for marker in PROXY_FAILURE_MARKERS):
        return all_domains
    if any(marker in text for marker in ACCOUNT_FAILURE_MARKERS):
        return all_domains[:1]
    return ()


def _record_failure_domains(
    session: Session,
    lease: AccountPoolConcurrencyLease,
    policy: ExecutionResiliencePolicyRevision,
    *,
    domains: tuple[tuple[str, str], ...],
    failure_code: str,
) -> None:
    observed_at = _now()
    window_start = observed_at - timedelta(seconds=int(policy.circuit_window_seconds))
    for kind, key in domains:
        state = _locked_or_new_state(
            session,
            lease,
            policy,
            kind=kind,
            key=key,
        )
        failures = _recent_failures(state.failure_times, window_start)
        failures.append(observed_at.isoformat())
        state.failure_times = failures
        state.last_failure_code = failure_code
        threshold = int(policy.circuit_failure_threshold)
        if state.state == "half_open" or len(failures) >= threshold:
            state.state = "open"
            state.opened_until = observed_at + timedelta(
                seconds=int(policy.circuit_open_seconds)
            )
        state.version = int(state.version or 0) + 1
        state.updated_at = observed_at


def _recent_failures(values: list[str], window_start: datetime) -> list[str]:
    recent = []
    for value in values or []:
        try:
            observed_at = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            continue
        if observed_at >= window_start:
            recent.append(observed_at.isoformat())
    return recent


def _locked_or_new_state(
    session: Session,
    lease: AccountPoolConcurrencyLease,
    policy: ExecutionResiliencePolicyRevision,
    *,
    kind: str,
    key: str,
) -> ExecutionCircuitState:
    state = _locked_state(session, lease.tenant_id, kind=kind, key=key)
    if state is not None:
        return state
    state = ExecutionCircuitState(
        tenant_id=lease.tenant_id,
        resilience_policy_revision_id=policy.id,
        domain_kind=kind,
        domain_key=key,
    )
    session.add(state)
    session.flush()
    return state


def _locked_state(
    session: Session,
    tenant_id: int,
    *,
    kind: str,
    key: str,
) -> ExecutionCircuitState | None:
    return session.scalar(
        select(ExecutionCircuitState)
        .where(
            ExecutionCircuitState.tenant_id == tenant_id,
            ExecutionCircuitState.domain_kind == kind,
            ExecutionCircuitState.domain_key == key,
        )
        .with_for_update()
    )


def lock_resilience_policy(
    session: Session,
    policy_id: str,
) -> ExecutionResiliencePolicyRevision:
    policy = session.scalar(
        select(ExecutionResiliencePolicyRevision)
        .where(ExecutionResiliencePolicyRevision.id == policy_id)
        .with_for_update()
    )
    if policy is None:
        raise RuntimeError("execution_resilience_policy_missing")
    return policy


def _domain_keys(
    account_id: int,
    route_key: str,
    egress_key: str,
) -> tuple[tuple[str, str], ...]:
    values = [("account", f"account:{account_id}")]
    if route_key:
        values.append(("proxy_route", route_key))
    if egress_key and egress_key != route_key:
        values.append(("proxy_egress", egress_key))
    return tuple(values)


__all__ = [
    "circuit_blocker",
    "lock_resilience_policy",
    "record_confirmed",
    "record_failed",
    "record_unknown",
]
