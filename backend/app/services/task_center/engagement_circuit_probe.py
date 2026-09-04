from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    ExecutionCircuitState,
    ExecutionResiliencePolicyRevision,
    HealthProbeAttempt,
    TgAccount,
    TgAccountAuthorization,
)
from app.services._common import _now, gateway
from app.services.account_runtime_transport import task_account_runtime_transport
from app.timezone import as_beijing

from .engagement_runtime_domains import proxy_domain_keys


PROBE_LEASE_SECONDS = 30
PROBE_BATCH_LIMIT = 20


@dataclass(frozen=True)
class CircuitProbeJob:
    attempt_id: str
    account_id: int | None
    session_ciphertext: str | None
    credentials: object | None
    connect_timeout_seconds: float
    gateway_timeout_seconds: float
    setup_error: str = ""


@dataclass(frozen=True)
class CircuitProbeResult:
    ok: bool
    outcome_code: str
    evidence: dict


def drain_due_circuit_probes(session_factory, *, limit: int = PROBE_BATCH_LIMIT) -> int:
    with session_factory() as session:
        circuit_ids = _due_circuit_ids(session, min(max(1, limit), PROBE_BATCH_LIMIT))
        session.commit()
    processed = 0
    for circuit_id in circuit_ids:
        job = _claim_probe(session_factory, circuit_id)
        if job is None:
            continue
        result = _run_probe(job)
        _settle_probe(session_factory, job, result)
        processed += 1
    return processed


def _due_circuit_ids(session: Session, limit: int) -> list[str]:
    current = _now()
    return list(
        session.scalars(
            select(ExecutionCircuitState.id)
            .where(
                or_(
                    and_(
                        ExecutionCircuitState.state == "open",
                        or_(
                            ExecutionCircuitState.opened_until.is_(None),
                            ExecutionCircuitState.opened_until <= current,
                        ),
                    ),
                    and_(
                        ExecutionCircuitState.state == "half_open",
                        or_(
                            ExecutionCircuitState.probe_lease_until.is_(None),
                            ExecutionCircuitState.probe_lease_until <= current,
                        ),
                    ),
                )
            )
            .order_by(ExecutionCircuitState.updated_at, ExecutionCircuitState.id)
            .limit(limit)
        )
    )


def _claim_probe(session_factory, circuit_id: str) -> CircuitProbeJob | None:
    with session_factory() as session:
        circuit = session.scalar(
            select(ExecutionCircuitState)
            .where(ExecutionCircuitState.id == circuit_id)
            .with_for_update()
        )
        if not _claimable(circuit, _now()):
            session.commit()
            return None
        _expire_previous_owner(session, circuit)
        account = _probe_account(session, circuit)
        job, attempt = _new_probe_job(session, circuit, account)
        session.add(attempt)
        circuit.state = "half_open"
        circuit.opened_until = None
        circuit.probe_attempt_id = attempt.id
        circuit.probe_lease_until = attempt.deadline_at
        circuit.version = int(circuit.version or 0) + 1
        circuit.updated_at = attempt.started_at
        attempt.circuit_version = circuit.version
        session.commit()
        return job


def _claimable(circuit: ExecutionCircuitState | None, current: datetime) -> bool:
    if circuit is None or circuit.state == "closed":
        return False
    normalized_current = as_beijing(current)
    if circuit.state == "open":
        return circuit.opened_until is None or as_beijing(circuit.opened_until) <= normalized_current
    if circuit.state == "half_open":
        return circuit.probe_lease_until is None or as_beijing(circuit.probe_lease_until) <= normalized_current
    return False


def _expire_previous_owner(session: Session, circuit: ExecutionCircuitState) -> None:
    if not circuit.probe_attempt_id:
        return
    previous = session.get(HealthProbeAttempt, circuit.probe_attempt_id)
    if previous is not None and previous.state in {"claimed", "running"}:
        previous.state = "expired"
        previous.completed_at = _now()
        previous.outcome_code = "probe_lease_expired"
    session.flush()


def _new_probe_job(
    session: Session,
    circuit: ExecutionCircuitState,
    account: TgAccount | None,
) -> tuple[CircuitProbeJob, HealthProbeAttempt]:
    policy = session.get(
        ExecutionResiliencePolicyRevision,
        circuit.resilience_policy_revision_id,
    )
    if policy is None:
        raise RuntimeError("execution_resilience_policy_missing")
    started_at = _now()
    attempt_id = str(uuid4())
    snapshot = _dependency_snapshot(session, account) if account else {}
    setup_error, ciphertext, credentials = _probe_material(session, account)
    attempt = HealthProbeAttempt(
        id=attempt_id,
        tenant_id=circuit.tenant_id,
        circuit_state_id=circuit.id,
        resilience_policy_revision_id=policy.id,
        circuit_version=int(circuit.version or 0) + 1,
        probe_revision=int(circuit.version or 0) + 1,
        domain_kind=circuit.domain_kind,
        domain_key=circuit.domain_key,
        account_id=account.id if account else None,
        dependency_snapshot=snapshot,
        owner_token=attempt_id,
        state="running",
        started_at=started_at,
        deadline_at=started_at + timedelta(seconds=PROBE_LEASE_SECONDS),
    )
    job = CircuitProbeJob(
        attempt_id=attempt_id,
        account_id=account.id if account else None,
        session_ciphertext=ciphertext,
        credentials=credentials,
        connect_timeout_seconds=float(policy.telegram_connect_timeout_seconds),
        gateway_timeout_seconds=float(policy.telegram_gateway_timeout_seconds),
        setup_error=setup_error,
    )
    return job, attempt


def _probe_material(
    session: Session,
    account: TgAccount | None,
) -> tuple[str, str | None, object | None]:
    if account is None:
        return "circuit_probe_account_unavailable", None, None
    try:
        transport = task_account_runtime_transport(session, account)
        return "", transport.session_ciphertext, transport.credentials
    except ValueError as exc:
        return str(exc) or "circuit_probe_credentials_unavailable", None, None


def _run_probe(job: CircuitProbeJob) -> CircuitProbeResult:
    if job.setup_error:
        return CircuitProbeResult(False, "probe_setup_failed", {"detail": job.setup_error})
    try:
        health = gateway.check_account_health_isolated(
            job.session_ciphertext,
            job.credentials,
            connect_timeout_seconds=job.connect_timeout_seconds,
            timeout_seconds=job.gateway_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - typed at the circuit boundary.
        return CircuitProbeResult(
            False,
            "probe_transport_failed",
            {"detail": str(exc).strip() or type(exc).__name__},
        )
    ok = health.status == AccountStatus.ACTIVE.value
    return CircuitProbeResult(
        ok,
        "probe_succeeded" if ok else "probe_account_unavailable",
        {
            "status": str(health.status or ""),
            "health_score": float(health.health_score or 0),
            "detail": str(health.detail or ""),
        },
    )


def _settle_probe(session_factory, job: CircuitProbeJob, result: CircuitProbeResult) -> None:
    with session_factory() as session:
        attempt = session.scalar(
            select(HealthProbeAttempt)
            .where(HealthProbeAttempt.id == job.attempt_id)
            .with_for_update()
        )
        if attempt is None:
            return
        circuit = session.scalar(
            select(ExecutionCircuitState)
            .where(ExecutionCircuitState.id == attempt.circuit_state_id)
            .with_for_update()
        )
        if circuit is None:
            return
        if not _probe_owner_current(circuit, attempt):
            _finish_attempt(attempt, "superseded", "probe_owner_superseded", result)
            session.commit()
            return
        if not _dependency_current(session, attempt):
            _finish_attempt(attempt, "superseded", "probe_dependency_changed", result)
            _requeue_changed_dependency(circuit)
            session.commit()
            return
        _finish_attempt(
            attempt,
            "succeeded" if result.ok else "failed",
            result.outcome_code,
            result,
        )
        _apply_probe_outcome(session, circuit, attempt, result.ok)
        session.commit()


def _probe_owner_current(
    circuit: ExecutionCircuitState,
    attempt: HealthProbeAttempt,
) -> bool:
    return (
        circuit.state == "half_open"
        and circuit.probe_attempt_id == attempt.id
        and int(circuit.version or 0) == int(attempt.circuit_version)
    )


def _dependency_current(session: Session, attempt: HealthProbeAttempt) -> bool:
    account = session.get(TgAccount, attempt.account_id) if attempt.account_id else None
    return bool(account and _dependency_snapshot(session, account) == attempt.dependency_snapshot)


def _finish_attempt(
    attempt: HealthProbeAttempt,
    state: str,
    outcome_code: str,
    result: CircuitProbeResult,
) -> None:
    attempt.state = state
    attempt.outcome_code = outcome_code
    attempt.evidence = dict(result.evidence or {})
    attempt.completed_at = _now()


def _requeue_changed_dependency(circuit: ExecutionCircuitState) -> None:
    circuit.state = "open"
    circuit.opened_until = _now()
    circuit.probe_attempt_id = None
    circuit.probe_lease_until = None
    circuit.last_failure_code = "probe_dependency_changed"
    circuit.version = int(circuit.version or 0) + 1
    circuit.updated_at = _now()


def _apply_probe_outcome(
    session: Session,
    circuit: ExecutionCircuitState,
    attempt: HealthProbeAttempt,
    ok: bool,
) -> None:
    current = _now()
    circuit.probe_attempt_id = None
    circuit.probe_lease_until = None
    if ok:
        circuit.state = "closed"
        circuit.opened_until = None
        circuit.failure_times = []
        circuit.last_failure_code = ""
    else:
        policy = session.get(
            ExecutionResiliencePolicyRevision,
            attempt.resilience_policy_revision_id,
        )
        if policy is None:
            raise RuntimeError("execution_resilience_policy_missing")
        circuit.state = "open"
        circuit.opened_until = current + timedelta(
            seconds=int(policy.circuit_open_seconds)
        )
        circuit.last_failure_code = attempt.outcome_code
    circuit.version = int(circuit.version or 0) + 1
    circuit.updated_at = current


def _probe_account(
    session: Session,
    circuit: ExecutionCircuitState,
) -> TgAccount | None:
    if circuit.domain_kind == "account":
        account_id = _account_id_from_key(circuit.domain_key)
        account = session.get(TgAccount, account_id) if account_id else None
        return account if _account_usable(account, circuit.tenant_id) else None
    candidates = session.scalars(
        select(TgAccount)
        .where(
            TgAccount.tenant_id == circuit.tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.status == AccountStatus.ACTIVE.value,
        )
        .order_by(TgAccount.id)
    )
    for account in candidates:
        route_key, egress_key = proxy_domain_keys(session, account)
        if circuit.domain_kind == "proxy_route" and route_key == circuit.domain_key:
            return account
        if circuit.domain_kind == "proxy_egress" and egress_key == circuit.domain_key:
            return account
    return None


def _account_id_from_key(domain_key: str) -> int | None:
    prefix, separator, raw = domain_key.partition(":")
    if prefix != "account" or not separator or not raw.isdigit():
        return None
    return int(raw)


def _account_usable(account: TgAccount | None, tenant_id: int) -> bool:
    return bool(
        account
        and account.tenant_id == tenant_id
        and account.deleted_at is None
        and account.status == AccountStatus.ACTIVE.value
    )


def _current_authorization(
    session: Session,
    account: TgAccount,
) -> TgAccountAuthorization | None:
    if not account.current_authorization_id:
        return None
    authorization = session.get(
        TgAccountAuthorization,
        account.current_authorization_id,
    )
    if authorization is None or not authorization.is_current:
        return None
    return authorization


def _dependency_snapshot(session: Session, account: TgAccount | None) -> dict:
    if account is None:
        return {}
    authorization = _current_authorization(session, account)
    route_key, egress_key = proxy_domain_keys(session, account)
    return {
        "account_id": account.id,
        "authorization_generation": int(account.authorization_generation or 0),
        "connection_generation": int(account.connection_generation or 0),
        "developer_app_version": int(account.developer_app_version or 0),
        "current_authorization_id": account.current_authorization_id,
        "authorization_fact_version": int(authorization.fact_version or 0)
        if authorization
        else 0,
        "authorization_proxy_id": authorization.proxy_id if authorization else None,
        "account_proxy_id": account.proxy_id,
        "proxy_route_key": route_key,
        "proxy_egress_key": egress_key,
    }


__all__ = ["drain_due_circuit_probes"]
