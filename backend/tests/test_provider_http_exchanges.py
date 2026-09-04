from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.ai_gateway import AiProviderCredentials
from app.ai_http_transport import AiHttpCallNotStarted, AiHttpResultUnknown, read_http
from app.ai_transport_errors import AiProviderResultUnknown
from app.database import Base
from app.models import AiProvider, AiProviderAttempt, GenerationJob, ProviderHttpExchange, ProviderHttpExchangeJob, Tenant
from app.services.task_center import generation_timing_binding, provider_http_exchanges as ledger
from app.services.task_center import ai_provider_candidate_runtime as drafts, ai_structured_provider_runtime as structured
from app.services.task_center.provider_http_tracking import TrackedProviderHttp, _scope_bindings
from tests.ai_http_test_support import local_http_server
from tests.test_generation_timing_binding import NOW, _bind, _job


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def environment(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'provider-http-test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(generation_timing_binding, "_now", lambda: NOW)
    monkeypatch.setattr(ledger, "_now", lambda: NOW)
    monkeypatch.setattr("app.services.task_center.generation_invocation_budget._now", lambda: NOW)
    with factory() as session:
        session.add(Tenant(id=1, name="QA HTTP ledger"))
        provider = AiProvider(id=1, provider_name="QA", base_url="http://localhost", model_name="mimo-v2.5", api_key_ciphertext="QA")
        session.add(provider)
        session.flush()
        task, job = _job(session, identity="job-a")
        task.status = "running"
        jobs, snapshots = [], []
        for identity in ("job-a", "job-b"):
            current = job if identity == "job-a" else _job(session, identity=identity)[1]
            current.state, current.generation_owner_id, current.generation_lease_epoch = "generating", "QA-worker", 7
            current.lease_expires_at = NOW + timedelta(minutes=5)
            jobs.append(current.id)
            snapshots.extend(_bind(session, task, current)["bindings"])
        session.commit()
    config = {"engagement_contract_version": "unified_engagement_v1", "_generation_job_id": jobs[0],
        "_ai_provider_invocation_key": "QA-stage", "_ai_execution_timing": {"version": "generation_timing_v1",
        "bindings": snapshots, "llm_timeout_ceiling_seconds": 5,
        "candidate_ready_deadline_at": (NOW + timedelta(minutes=3)).isoformat()}}
    yield SimpleNamespace(factory=factory, config=config, provider=provider, jobs=jobs)
    engine.dispose()


def _tracker(environment, *, transport=read_http, chain="QA-chain"):
    scope = ledger.ExchangeScope(tuple(environment.config["_ai_execution_timing"]["bindings"]), 1, "mimo-v2.5", "QA", "QA-logical")
    return TrackedProviderHttp(environment.factory, scope, transport, chain)


def _request(tracker):
    return tracker(urllib.request.Request("http://localhost/QA", data=b"QA"), timeout=5, request_deadline=time.monotonic() + 5)


def _rows(environment):
    with environment.factory() as session:
        return list(session.scalars(select(ProviderHttpExchange).order_by(ProviderHttpExchange.started_at, ProviderHttpExchange.id)))


def _settle(environment, *, chain="QA-chain", outcome="success"):
    with environment.factory() as session, session.begin():
        ledger.settle_provider_exchanges(session, environment.config, provider_id=1, request_id="QA-logical", outcome=outcome, chain_id=chain)


def test_committed_before_network_and_batch_is_one_exchange(environment):
    def observed(request, **kwargs):
        assert [row.outcome for row in _rows(environment)] == ["started"]
        with environment.factory() as session:
            assert len(list(session.scalars(select(ProviderHttpExchangeJob)))) == 2
        return b"QA response"
    assert _request(_tracker(environment, transport=observed)) == b"QA response"
    assert [row.outcome for row in _rows(environment)] == ["response_received"]
    _settle(environment)
    _settle(environment)
    assert [row.outcome for row in _rows(environment)] == ["settled"]


def test_internal_repair_can_continue_but_another_chain_cannot(environment):
    tracker = _tracker(environment, transport=lambda *args, **kwargs: b"QA")
    _request(tracker)
    _request(tracker)
    with pytest.raises(AiProviderResultUnknown, match="previous_exchange_unresolved"):
        _request(replace(tracker, chain_id="other"))
    assert len(_rows(environment)) == 2
    _settle(environment, chain="other", outcome="provider_result_unknown")
    assert {row.outcome for row in _rows(environment)} == {"response_received"}
    _settle(environment)
    _request(replace(tracker, chain_id="successor"))
    assert len(_rows(environment)) == 3


@pytest.mark.parametrize("error", (AiHttpCallNotStarted("QA"), urllib.error.URLError(ConnectionRefusedError("QA"))))
def test_definite_pre_call_failure_is_not_unknown(environment, error):
    def fail(*args, **kwargs):
        raise error
    with pytest.raises(type(error)):
        _request(_tracker(environment, transport=fail))
    assert _rows(environment)[0].outcome == "not_started"


def test_unknown_survives_semantic_settlement_and_blocks_replay(environment):
    error = AiHttpResultUnknown("QA timeout", process_id=1, termination_confirmed=True)
    def fail(*args, **kwargs):
        raise error
    tracker = _tracker(environment, transport=fail)
    with pytest.raises(AiHttpResultUnknown):
        _request(tracker)
    _settle(environment)
    with pytest.raises(AiProviderResultUnknown, match="previous_exchange_unresolved"):
        _request(tracker)
    row = _rows(environment)[0]
    assert row.outcome == "unknown" and row.local_termination_confirmed is True


def test_pre_commit_failure_has_zero_http_calls(environment):
    calls = []
    class FailingSession(Session):
        def flush(self, *args, **kwargs):
            raise RuntimeError("QA write unavailable")
    factory = sessionmaker(bind=environment.factory.kw["bind"], class_=FailingSession, autoflush=False)
    tracker = replace(_tracker(environment, transport=lambda *args, **kwargs: calls.append(True)), session_factory=factory)
    with pytest.raises(RuntimeError, match="QA write unavailable"):
        _request(tracker)
    assert calls == [] and _rows(environment) == []


def test_response_record_failure_retains_started_and_cause(environment, monkeypatch):
    tracker = _tracker(environment, transport=lambda *args, **kwargs: b"QA")
    original = ledger.receive_exchange
    def fail_factory():
        raise RuntimeError("QA settlement storage unavailable")
    monkeypatch.setattr("app.services.task_center.provider_http_tracking.receive_exchange",
        lambda _factory, identity, **facts: original(fail_factory, identity, **facts))
    with pytest.raises(AiProviderResultUnknown, match="persistence_unproven") as caught:
        _request(tracker)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert _rows(environment)[0].outcome == "started"


@pytest.mark.parametrize("case", (("generation_owner_id", "new-worker", "owner_stale"),
    ("generation_lease_epoch", 8, "owner_stale"), ("state", "unknown", "owner_stale"),
    ("lease_expires_at", NOW, "lease_expired")))
def test_stale_worker_cannot_issue(environment, case):
    field, value, reason = case
    with environment.factory() as session, session.begin():
        setattr(session.get(GenerationJob, environment.jobs[0]), field, value)
    with pytest.raises(ValueError, match=reason):
        _request(_tracker(environment, transport=lambda *args, **kwargs: pytest.fail("HTTP must not run")))
    assert not _rows(environment)


def test_slot_subset_and_no_mutation(environment):
    config = {**environment.config, "generation_slots": [
        {"slot_id": "a", "generation_job_id": "job-a"}, {"slot_id": "b", "generation_job_id": "job-b"}],
        "_provider_http_slot_ids": ["b"]}
    original = deepcopy(config)
    assert [item["generation_job_id"] for item in _scope_bindings(config)] == ["job-b"]
    assert config == original
    with pytest.raises(ValueError, match="slot_scope_missing"):
        _scope_bindings({**config, "_provider_http_slot_ids": ["missing"]})


@pytest.mark.parametrize("kind", ("reasoning", "parse"))
def test_actual_draft_runtime_counts_internal_http_exchanges(environment, monkeypatch, kind):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setattr(drafts, "begin_provider_call", lambda *_: None)
    final = {"choices": [{"message": {"content": '{"drafts":[{"content":"QA 完整回复"}]}'}}]}
    first = {"choices": [{"message": {"content": "" if kind == "reasoning" else '{"drafts":[',
        "reasoning_content": "QA reasoning"}, "finish_reason": "length"}]}
    responses = [first, final]
    def response(_path):
        assert sum(row.outcome == "started" for row in _rows(environment)) == 1
        return json.dumps(responses.pop(0)).encode()
    with local_http_server(response_body=response) as (url, observed), environment.factory() as session:
        credentials = AiProviderCredentials("QA", "openai_compatible", url, "mimo-v2.5", "QA")
        monkeypatch.setattr(drafts, "draft_provider_calls", lambda *_: ([environment.provider], [(environment.provider, credentials)]))
        request = drafts.ProviderDraftRequest("请输出 json drafts", 1, "QA", "QA", ("自动化助手",), 0.7, 512, "QA", 5, "QA-logical")
        policy = drafts.ProviderCandidatePolicy("mimo-v2.5", "", False, "QA", True, attempt_config=environment.config)
        result = drafts.generate_with_provider_candidates(session, environment.provider, request, policy=policy)
        assert [item.content for item in result.candidates] == ["QA 完整回复"]
        assert len(observed) == 2
        assert len(list(session.scalars(select(AiProviderAttempt)))) == 1
        assert len(list(session.scalars(select(ProviderHttpExchangeJob)))) == 4
    rows = _rows(environment)
    assert len(rows) == 2 and {row.outcome for row in rows} == {"settled"}
    assert not hasattr(drafts.ai_gateway, "_http_transport")


@pytest.mark.parametrize("antigravity", (False, True))
def test_actual_structured_runtime_uses_tracked_transport(environment, monkeypatch, antigravity):
    from app.services.antigravity_provider_client import ANTIGRAVITY_PRIMARY_MODEL
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setattr(structured, "begin_provider_call", lambda *_: None)
    payload = {"state": "confirmed", "structured_output": {"ok": True}} if antigravity else {
        "choices": [{"message": {"content": '{"ok":true}'}}]}
    with local_http_server(response_body=lambda _: json.dumps(payload).encode()) as (url, observed), environment.factory() as session:
        credentials = AiProviderCredentials("QA", "antigravity_cli" if antigravity else "openai_compatible", url,
            ANTIGRAVITY_PRIMARY_MODEL if antigravity else "mimo-v2.5", "QA")
        monkeypatch.setattr(structured, "structured_provider_calls", lambda *_: ([environment.provider], [(environment.provider, credentials)]))
        request = structured.StructuredProviderRequest("QA", "QA", environment.config, 0.7, 512, 1, "QA", credentials.model_name, "primary", "")
        payload, _tokens = structured.generate_structured_with_candidates(session, environment.provider, request)
        assert payload == {"ok": True} and len(observed) == 1
    assert _rows(environment)[0].outcome == "settled"


def test_antigravity_unknown_response_keeps_exchange_unresolved(environment, monkeypatch):
    from app.services.antigravity_provider_client import ANTIGRAVITY_PRIMARY_MODEL
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setattr(structured, "begin_provider_call", lambda *_: None)
    with local_http_server(response_body=lambda _: b'{"state":"unknown"}') as (url, observed), environment.factory() as session:
        credentials = AiProviderCredentials("QA", "antigravity_cli", url, ANTIGRAVITY_PRIMARY_MODEL, "QA")
        monkeypatch.setattr(structured, "structured_provider_calls", lambda *_: ([environment.provider], [(environment.provider, credentials)]))
        request = structured.StructuredProviderRequest("QA", "QA", environment.config, 0.7, 512, 1, "QA", credentials.model_name, "primary", "")
        with pytest.raises(AiProviderResultUnknown):
            structured.generate_structured_with_candidates(session, environment.provider, request)
        assert len(observed) == 1
    assert _rows(environment)[0].outcome == "unknown"


@pytest.mark.parametrize("outcome", ("success", "failed", "provider_result_unknown"))
def test_attempt_commit_failure_does_not_release_exchange(environment, monkeypatch, outcome):
    from app.services.task_center.ai_provider_attempts import record_provider_attempt
    _request(_tracker(environment, transport=lambda *args, **kwargs: b"QA"))
    with environment.factory() as session:
        def fail():
            raise RuntimeError("QA attempt commit failed")
        monkeypatch.setattr(session, "commit", fail)
        with pytest.raises(AiProviderResultUnknown, match="exchange_settlement_unproven"):
            record_provider_attempt(session, environment.config, environment.provider, purpose="QA", priority=1,
                model_name="mimo-v2.5", request_text="QA", outcome=outcome, provider_request_id="QA-logical", http_chain_id="QA-chain")
    assert _rows(environment)[0].outcome == "response_received"
    with environment.factory() as session:
        assert not list(session.scalars(select(AiProviderAttempt)))


def test_unrelated_job_can_run_while_other_job_is_unknown(environment):
    base = _tracker(environment, transport=lambda *args, **kwargs: b"QA")
    first = replace(base, scope=replace(base.scope, job_bindings=(base.scope.job_bindings[0],)))
    second = replace(base, scope=replace(base.scope, job_bindings=(base.scope.job_bindings[1],)), chain_id="other-job")
    _request(first)
    _settle(environment, outcome="provider_result_unknown")
    _request(second)
    assert {row.outcome for row in _rows(environment)} == {"unknown", "response_received"}


@pytest.mark.parametrize("change", ({"status": "paused"}, {"task_lifecycle_epoch": 2}))
def test_stopped_or_replaced_task_cannot_issue(environment, change):
    from app.models import Task
    with environment.factory() as session, session.begin():
        task = session.get(Task, "group_ai_chat")
        for key, value in change.items():
            setattr(task, key, value)
    with pytest.raises(ValueError, match="task_owner_stale"):
        _request(_tracker(environment, transport=lambda *args, **kwargs: pytest.fail("HTTP must not run")))
    assert not _rows(environment)


def test_row_lock_contention_is_admission_not_quality_failure(environment, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from app.services.task_center.provider_admission import ProviderAdmissionBlocked
    error = OperationalError("QA lock", {}, SimpleNamespace(sqlstate="55P03"))
    def busy(*args, **kwargs):
        raise error
    monkeypatch.setattr(ledger, "_lock_jobs", busy)
    with pytest.raises(ProviderAdmissionBlocked) as caught:
        _request(_tracker(environment, transport=lambda *args, **kwargs: pytest.fail("HTTP must not run")))
    assert caught.value.reason == "provider_exchange_admission_busy"
    assert not _rows(environment)


def test_real_slow_http_records_unknown_and_prevents_second_network_call(environment, monkeypatch):
    from tests.ai_http_test_support import HTTP_IO_TEST_BUDGET_SECONDS
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setattr(structured, "begin_provider_call", lambda *_: None)
    timing = {**environment.config["_ai_execution_timing"], "llm_timeout_ceiling_seconds": HTTP_IO_TEST_BUDGET_SECONDS}
    config = {**environment.config, "_ai_execution_timing": timing}
    with local_http_server() as (url, observed), environment.factory() as session:
        credentials = AiProviderCredentials("QA", "openai_compatible", url + "/drip", "mimo-v2.5", "QA")
        monkeypatch.setattr(structured, "structured_provider_calls", lambda *_: ([environment.provider], [(environment.provider, credentials)]))
        request = structured.StructuredProviderRequest("QA", "QA", config, 0.7, 512, 1, "QA", "mimo-v2.5", "primary", "")
        with pytest.raises(AiHttpResultUnknown):
            structured.generate_structured_with_candidates(session, environment.provider, request)
        with pytest.raises(AiProviderResultUnknown, match="previous_exchange_unresolved"):
            structured.generate_structured_with_candidates(session, environment.provider, request)
        assert len(observed) == 1
    rows = _rows(environment)
    assert len(rows) == 1 and rows[0].outcome == "unknown"
    assert rows[0].local_termination_confirmed
