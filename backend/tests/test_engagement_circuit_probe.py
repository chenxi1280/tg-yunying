from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountStatus,
    ExecutionCircuitState,
    ExecutionResiliencePolicyRevision,
    HealthProbeAttempt,
    Tenant,
    TgAccount,
)
from app.services._common import _now
from app.services.task_center import engagement_circuit_probe as probes
from app.services.task_center.engagement_runtime_circuit import (
    circuit_blocker,
    record_confirmed,
)


pytestmark = pytest.mark.no_postgres


def _database():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TgAccount(
                id=11,
                tenant_id=1,
                display_name="账号11",
                phone_masked="11",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="cipher",
            )
        )
        policy = ExecutionResiliencePolicyRevision(tenant_id=1)
        session.add(policy)
        session.flush()
        circuit = ExecutionCircuitState(
            tenant_id=1,
            resilience_policy_revision_id=policy.id,
            domain_kind="account",
            domain_key="account:11",
            state="open",
            opened_until=_now() - timedelta(seconds=1),
        )
        session.add(circuit)
        session.commit()
    return engine


def _session_factory(engine):
    return lambda: Session(engine)


def test_expired_open_circuit_still_blocks_business_until_probe_closes(
    monkeypatch,
) -> None:
    engine = _database()
    observed: dict = {}

    with Session(engine) as session:
        blocker = circuit_blocker(
            session,
            tenant_id=1,
            account_id=11,
            route_key="",
            egress_key="",
        )
        circuit = session.scalar(select(ExecutionCircuitState))
        assert blocker and blocker[0] == "execution_circuit_probe_pending"
        assert circuit is not None and circuit.state == "open"

    monkeypatch.setattr(
        probes,
        "_probe_material",
        lambda *_args: ("", "cipher", object()),
    )

    def healthy_probe(*_args, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(
            status=AccountStatus.ACTIVE.value,
            health_score=99,
            detail="healthy",
        )

    monkeypatch.setattr(probes.gateway, "check_account_health_isolated", healthy_probe)

    assert probes.drain_due_circuit_probes(_session_factory(engine)) == 1

    with Session(engine) as session:
        circuit = session.scalar(select(ExecutionCircuitState))
        attempt = session.scalar(select(HealthProbeAttempt))
        assert circuit is not None and circuit.state == "closed"
        assert circuit.probe_attempt_id is None
        assert attempt is not None and attempt.state == "succeeded"
        assert attempt.outcome_code == "probe_succeeded"
    assert observed["connect_timeout_seconds"] == 5
    assert observed["timeout_seconds"] == 10


def test_probe_success_cannot_close_circuit_after_dependency_revision_changes(
    monkeypatch,
) -> None:
    engine = _database()
    monkeypatch.setattr(
        probes,
        "_probe_material",
        lambda *_args: ("", "cipher", object()),
    )

    def stale_success(*_args, **_kwargs):
        with Session(engine) as session:
            account = session.get(TgAccount, 11)
            account.connection_generation += 1
            session.commit()
        return SimpleNamespace(
            status=AccountStatus.ACTIVE.value,
            health_score=99,
            detail="old route healthy",
        )

    monkeypatch.setattr(probes.gateway, "check_account_health_isolated", stale_success)

    assert probes.drain_due_circuit_probes(_session_factory(engine)) == 1

    with Session(engine) as session:
        circuit = session.scalar(select(ExecutionCircuitState))
        attempt = session.scalar(select(HealthProbeAttempt))
        assert circuit is not None and circuit.state == "open"
        assert circuit.opened_until <= _now()
        assert attempt is not None and attempt.state == "superseded"
        assert attempt.outcome_code == "probe_dependency_changed"


def test_active_half_open_owner_is_not_probed_twice(monkeypatch) -> None:
    engine = _database()
    with Session(engine) as session:
        circuit = session.scalar(select(ExecutionCircuitState))
        circuit.state = "half_open"
        circuit.opened_until = None
        circuit.probe_attempt_id = "current-owner"
        circuit.probe_lease_until = _now() + timedelta(seconds=20)
        session.commit()
    monkeypatch.setattr(
        probes.gateway,
        "check_account_health_isolated",
        lambda *_args, **_kwargs: pytest.fail("active owner must remain single"),
    )

    assert probes.drain_due_circuit_probes(_session_factory(engine)) == 0


def test_business_success_does_not_close_an_open_circuit() -> None:
    engine = _database()
    with Session(engine) as session:
        policy = session.scalar(select(ExecutionResiliencePolicyRevision))
        circuit = session.scalar(select(ExecutionCircuitState))
        lease = SimpleNamespace(
            tenant_id=1,
            account_id=11,
            proxy_route_key="",
            proxy_egress_key="",
        )
        fence = SimpleNamespace(resilience_policy_revision_id=policy.id)

        record_confirmed(session, lease, fence)
        session.flush()

        assert circuit is not None and circuit.state == "open"
