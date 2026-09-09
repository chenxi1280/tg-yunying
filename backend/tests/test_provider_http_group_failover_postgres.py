"""PostgreSQL proves the candidate exception cannot escape physical lineage."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.ai_transport_errors import AiProviderResultUnknown
from app.models import AiProvider, GenerationJob, ProviderHttpExchange, ProviderHttpExchangeJob, Tenant
from app.services.task_center import generation_timing_binding, provider_http_exchanges as ledger
from app.services.task_center import provider_http_failover as failover
from app.services.task_center.provider_admission import ProviderAdmissionBlocked
from tests.postgres_pacing_e4_fixture import factory as factory
from tests.test_generation_timing_binding import NOW, _bind, _job
from tests.test_provider_http_group_failover import _enable, _scope, _unknown


pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


@pytest.fixture
def environment(factory, monkeypatch):
    monkeypatch.setattr(generation_timing_binding, "_now", lambda: NOW)
    monkeypatch.setattr(ledger, "_now", lambda: NOW)
    monkeypatch.setattr(failover, "_now", lambda: NOW)
    with factory() as session:
        session.add(Tenant(id=1, name="QA failover PG"))
        session.flush()
        provider = AiProvider(id=1, provider_name="QA", base_url="http://localhost",
                              model_name="mimo-v2.5", api_key_ciphertext="QA")
        session.add(provider)
        session.flush()
        task, job = _job(session, identity="job-a")
        task.status = "running"
        job.state, job.generation_owner_id, job.generation_lease_epoch = "generating", "QA-worker", 7
        job.lease_expires_at = NOW + timedelta(minutes=5)
        timing = _bind(session, task, job)
        session.commit()
    environment = SimpleNamespace(factory=factory, jobs=["job-a"], provider=provider,
        config={"engagement_contract_version": "unified_engagement_v1", "_ai_execution_timing": timing})
    _enable(environment, monkeypatch)
    return environment


def test_pg_current_job_unknown_remains_and_second_inflight_candidate_blocks_third(environment):
    scope = _scope(environment)
    old_id = _unknown(environment, scope)
    next_scope = replace(scope, provider_id=2, logical_request_id="candidate-two")
    new_id = ledger.start_exchange(environment.factory, next_scope, chain_id="second", request_hash="b" * 64)
    with pytest.raises(AiProviderResultUnknown, match="previous_exchange_unresolved"):
        ledger.start_exchange(environment.factory, replace(next_scope, logical_request_id="third"),
                              chain_id="third", request_hash="c" * 64)
    with environment.factory() as session:
        rows = {row.id: row for row in session.scalars(select(ProviderHttpExchange))}
        assert rows[old_id].outcome == "unknown" and rows[old_id].local_termination_confirmed
        assert rows[new_id].outcome == "started"
        assert len(list(session.scalars(select(ProviderHttpExchangeJob)))) == 2
        assert session.get(GenerationJob, "job-a").state == "generating"


@pytest.mark.parametrize("new_epoch", (False, True))
def test_pg_successor_cannot_escape_historically_terminal_job_with_unknown_http(environment, new_epoch):
    scope = _scope(environment)
    old_id = _unknown(environment, scope)
    with environment.factory() as session:
        old = session.get(GenerationJob, "job-a")
        # Reproduce historical invalidation; the unknown HTTP itself remains untouched.
        old.state = "failed"
        task, successor = _job(session, identity="successor")
        if new_epoch:
            task.task_lifecycle_epoch += 1
        successor.task_lifecycle_epoch = task.task_lifecycle_epoch
        successor.obligation_type, successor.obligation_id = old.obligation_type, old.obligation_id
        successor.provider_route_snapshots = dict(old.provider_route_snapshots)
        successor.generation_sequence = 2
        successor.state, successor.generation_owner_id, successor.generation_lease_epoch = "generating", "QA-next", 8
        successor.lease_expires_at = NOW + timedelta(minutes=5)
        timing = _bind(session, task, successor)
        session.commit()
    next_scope = replace(scope, provider_id=2, logical_request_id="successor-candidate",
                         job_bindings=tuple(timing["bindings"]))
    with pytest.raises(AiProviderResultUnknown, match="previous_exchange_unresolved"):
        ledger.start_exchange(environment.factory, next_scope, chain_id="successor", request_hash="b" * 64)
    with environment.factory() as session:
        assert session.get(GenerationJob, "job-a").state == "failed"
        assert [row.id for row in session.scalars(select(ProviderHttpExchange))] == [old_id]


def test_pg_job_lock_is_retained_before_failover_and_releases_after_transaction(environment):
    scope = _scope(environment)
    _unknown(environment, scope)
    next_scope = replace(scope, provider_id=2, logical_request_id="next-candidate")
    with environment.factory() as owner:
        owner.scalar(select(GenerationJob).where(GenerationJob.id == "job-a").with_for_update())
        with pytest.raises(ProviderAdmissionBlocked):
            ledger.start_exchange(environment.factory, next_scope, chain_id="next", request_hash="b" * 64)
        owner.rollback()
    assert ledger.start_exchange(environment.factory, next_scope, chain_id="next", request_hash="b" * 64)
