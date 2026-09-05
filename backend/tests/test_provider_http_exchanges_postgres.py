"""Real migrated PostgreSQL transactions; no external Provider/Telegram traffic."""
from dataclasses import replace
from datetime import timedelta
import time
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.ai_transport_errors import AiProviderResultUnknown
from app.database import Base, SessionLocal, engine
from app.models import (
    AiProvider, ExecutionResiliencePolicyRevision, GenerationJob,
    GenerationTimingBinding, ProviderHttpExchange, ProviderHttpExchangeJob, Task, Tenant,
)
from app.services.task_center import generation_timing_binding, provider_http_exchanges as ledger
from app.services.task_center.provider_admission import ProviderAdmissionBlocked
from tests.test_generation_timing_binding import NOW, _bind, _job
from migrations.legacy_bootstrap import LEGACY_BOOTSTRAP_TABLES


# Rule assignment is outside this persistence contract; all FK parents are real.
pytestmark = pytest.mark.allow_missing_rule_binding
LOCK_RETURN_LIMIT_SECONDS = 2


@pytest.fixture(scope="module")
def provider_id():
    with SessionLocal() as session:
        if session.get(Tenant, 1) is None:
            session.add(Tenant(id=1, name="QA PostgreSQL exchange"))
            session.flush()
        policy = session.scalar(select(ExecutionResiliencePolicyRevision).where(
            ExecutionResiliencePolicyRevision.tenant_id == 1,
            ExecutionResiliencePolicyRevision.state == "active",
        ))
        if policy is None:
            session.add(ExecutionResiliencePolicyRevision(
                tenant_id=1, effective_from=NOW - timedelta(days=1)))
        provider = AiProvider(provider_name="QA local only", base_url="http://localhost",
            model_name="QA", api_key_ciphertext="QA-not-a-key")
        session.add(provider)
        session.flush()
        identity = provider.id
        task, job = _job(session, identity=str(uuid4()))
        task.status = "running"
        session.commit()
        return identity


@pytest.fixture
def scope(provider_id, monkeypatch):
    monkeypatch.setattr(generation_timing_binding, "_now", lambda: NOW)
    monkeypatch.setattr(ledger, "_now", lambda: NOW)
    snapshots = []
    with SessionLocal() as session:
        for _ in range(2):
            task, job = _job(session, identity=str(uuid4()))
            task.status = "running"
            job.state = "generating"
            job.generation_owner_id = "QA-worker"
            job.generation_lease_epoch = 7
            job.lease_expires_at = NOW + timedelta(minutes=5)
            snapshots.extend(_bind(session, task, job)["bindings"])
        session.commit()
    return ledger.ExchangeScope(tuple(snapshots), provider_id, "QA", "QA", str(uuid4()))


def _start(scope, *, chain="QA-chain"):
    return ledger.start_exchange(SessionLocal, scope, chain_id=chain, request_hash="a" * 64)


def _config(scope):
    return {"engagement_contract_version": "unified_engagement_v1",
            "_ai_execution_timing": {"bindings": scope.job_bindings}}


def _settle(session, scope, *, chain="QA-chain"):
    ledger.settle_provider_exchanges(session, _config(scope), provider_id=scope.provider_id,
        request_id=scope.logical_request_id, outcome="success", chain_id=chain)


def test_full_migration_head_and_new_table_orm_parity(scope):
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0224_legacy_account_occupancy"
        assert connection.scalar(text("SELECT current_database()")) == "tg_yunying_test"
        for model in (GenerationTimingBinding, ProviderHttpExchange, ProviderHttpExchangeJob):
            actual = {c["name"]: c["nullable"] for c in inspect(connection).get_columns(model.__tablename__)}
            assert actual == {c.name: c.nullable for c in model.__table__.columns}


def test_all_engine_tables_match_migrated_columns_and_foreign_keys(scope):
    with engine.connect() as connection:
        inspector = inspect(connection)
        for name in sorted(set(Base.metadata.tables) - LEGACY_BOOTSTRAP_TABLES):
            table = Base.metadata.tables[name]
            actual = {c["name"]: c["nullable"] for c in inspector.get_columns(name)}
            assert actual == {c.name: c.nullable for c in table.columns}, name
            actual_fks = {(tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
                          for fk in inspector.get_foreign_keys(name)}
            expected_fks = {(tuple(fk.columns.keys()), fk.referred_table.name,
                             tuple(element.column.name for element in fk.elements))
                            for fk in table.foreign_key_constraints}
            assert actual_fks == expected_fks, name


def test_job_nowait_rejects_contender_without_creating_exchange(scope):
    with SessionLocal() as holder:
        holder.scalar(select(GenerationJob).where(
            GenerationJob.id == scope.job_bindings[0]["generation_job_id"]).with_for_update())
        start = time.monotonic()
        with pytest.raises(ProviderAdmissionBlocked, match="provider_exchange_admission_busy"):
            _start(scope)
        assert time.monotonic() - start < LOCK_RETURN_LIMIT_SECONDS
        with SessionLocal() as observer:
            assert observer.scalar(select(ProviderHttpExchange.id).where(
                ProviderHttpExchange.logical_request_id == scope.logical_request_id)) is None
    assert _start(scope)


def test_task_writer_blocks_admission_but_shared_reader_does_not(scope):
    with SessionLocal() as holder:
        holder.scalar(select(Task).where(Task.id == scope.job_bindings[0]["task_id"]).with_for_update())
        with pytest.raises(ProviderAdmissionBlocked, match="provider_exchange_admission_busy"):
            _start(scope)
    with SessionLocal() as holder:
        holder.scalar(select(Task).where(Task.id == scope.job_bindings[0]["task_id"]).with_for_update(read=True))
        assert _start(scope)


def test_disjoint_job_not_blocked_by_other_job_lock(scope):
    first = replace(scope, job_bindings=scope.job_bindings[:1])
    second = replace(scope, job_bindings=scope.job_bindings[1:])
    with SessionLocal() as holder:
        ledger._lock_jobs(holder, first)
        assert _start(second)
        with pytest.raises(ProviderAdmissionBlocked):
            _start(first)


def test_started_is_durable_and_unknown_survives_caller_rollback(scope):
    with SessionLocal() as caller:
        exchange_id = _start(scope)
        caller.rollback()
    with SessionLocal() as observer:
        row = observer.get(ProviderHttpExchange, exchange_id)
        assert row.outcome == "started"
        assert row.started_at.utcoffset() == timedelta(hours=8)
        assert row.started_at.replace(tzinfo=None) == NOW
        assert len(list(observer.scalars(select(ProviderHttpExchangeJob).where(
            ProviderHttpExchangeJob.exchange_id == exchange_id)))) == 2
    ledger.receive_exchange(SessionLocal, exchange_id, outcome="unknown", local_termination_confirmed=True)
    with pytest.raises(AiProviderResultUnknown, match="previous_exchange_unresolved"):
        _start(scope, chain="successor")
    with SessionLocal() as observer:
        assert observer.get(ProviderHttpExchange, exchange_id).outcome == "unknown"


def test_response_settlement_is_atomic_with_caller_transaction(scope):
    exchange_id = _start(scope)
    ledger.receive_exchange(SessionLocal, exchange_id, outcome="response_received")
    with SessionLocal() as caller:
        _settle(caller, scope)
        caller.rollback()
    with SessionLocal() as observer:
        assert observer.get(ProviderHttpExchange, exchange_id).outcome == "response_received"
    with SessionLocal() as caller:
        _settle(caller, scope, chain="wrong-chain")
        caller.commit()
    with pytest.raises(AiProviderResultUnknown):
        _start(scope, chain="successor")
    with SessionLocal() as caller:
        _settle(caller, scope)
        caller.commit()
    assert _start(scope, chain="successor")


def test_foreign_key_failure_rolls_back_exchange_and_links(scope):
    with pytest.raises(IntegrityError):
        _start(replace(scope, provider_id=-987654321))
    with SessionLocal() as observer:
        assert observer.scalar(select(ProviderHttpExchange.id).where(
            ProviderHttpExchange.logical_request_id == scope.logical_request_id)) is None
    assert _start(scope)


def test_unresolved_generation_lineage_blocks_successor_on_postgres(scope):
    exchange_id = _start(scope)
    with SessionLocal() as session:
        old = session.get(GenerationJob, scope.job_bindings[0]["generation_job_id"])
        old.state = "failed"
        session.flush()
        task, successor = _job(session, identity=str(uuid4()))
        successor.obligation_type, successor.obligation_id = old.obligation_type, old.obligation_id
        successor.generation_sequence = old.generation_sequence + 1
        successor.state, successor.generation_owner_id = "generating", "QA-successor"
        successor.generation_lease_epoch = 8
        successor.lease_expires_at = NOW + timedelta(minutes=5)
        bindings = tuple(_bind(session, task, successor)["bindings"])
        session.commit()
    with pytest.raises(AiProviderResultUnknown, match="previous_exchange_unresolved"):
        _start(replace(scope, job_bindings=bindings), chain="successor")
    with SessionLocal() as session:
        assert session.get(ProviderHttpExchange, exchange_id).outcome == "started"
        indexes = inspect(session.connection()).get_indexes("generation_jobs")
        assert any(index["name"] == "ix_generation_job_provider_lineage" for index in indexes)
