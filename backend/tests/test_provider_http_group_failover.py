"""A new approved candidate retains the original unknown physical lineage."""
from dataclasses import replace
from datetime import timedelta
import json

import pytest
from sqlalchemy import select

from app.ai_gateway import AiProviderCredentials
from app.ai_transport_errors import AiProviderResultUnknown
from app.models import AiProvider, AiProviderAttempt, GenerationJob, Task, TenantAiProviderRouteSet
from app.services.task_center import provider_http_exchanges as ledger
from app.services.task_center import provider_http_failover as failover
from app.services.task_center import ai_provider_candidate_runtime as drafts, ai_structured_provider_runtime as structured
from tests.ai_http_test_support import local_http_server
from tests.test_generation_timing_binding import NOW
from tests.test_provider_http_exchanges import environment, _request, _rows, _tracker


pytestmark = pytest.mark.no_postgres
PURPOSE = "group_realize_general"


def _enable(environment, monkeypatch):
    monkeypatch.setattr(failover, "_now", lambda: NOW)
    with environment.factory() as session:
        provider = AiProvider(id=2, provider_name="QA alternate", base_url="http://localhost",
                              model_name="mimo-v2.5", api_key_ciphertext="QA")
        session.add(provider)
        session.flush()
        for purpose in ("group_context_route", PURPOSE, "group_semantic_review"):
            session.add(TenantAiProviderRouteSet(id=purpose, tenant_id=1, purpose=purpose,
                revision=1, content_hash="a" * 64, status="active"))
        for identity in environment.jobs:
            job = session.get(GenerationJob, identity)
            job.provider_route_snapshots = {purpose: {**value, "candidates": [
                {"provider_id": index, "priority": index, "model_name": "mimo-v2.5"} for index in (1, 2)]}
                for purpose, value in job.provider_route_snapshots.items()}
        session.commit()
    return {**environment.config, "_ai_group_emergency_enabled": True,
        "_ai_provider_route_provider_ids": [1, 2], "_ai_provider_route_models": {1: "mimo-v2.5", 2: "mimo-v2.5"},
        "_ai_provider_route_set_id": PURPOSE, "_ai_provider_route_set_revision": 1,
        "_ai_provider_route_set_hash": "a" * 64, "_ai_provider_route_purpose": PURPOSE}, provider


def _scope(environment):
    return replace(_tracker(environment).scope, purpose=PURPOSE, emergency_enabled=True,
                   route_set_id=PURPOSE, route_set_revision=1, route_set_hash="a" * 64)


def _unknown(environment, scope, *, outcome="unknown", terminated=True):
    identity = ledger.start_exchange(environment.factory, scope, chain_id="first-chain", request_hash="a" * 64)
    if outcome != "started":
        ledger.receive_exchange(environment.factory, identity, outcome=outcome, local_termination_confirmed=terminated)
    return identity


def test_approved_next_candidate_keeps_unknown_then_allows_distinct_later_stage(environment, monkeypatch):
    _enable(environment, monkeypatch)
    original = _scope(environment)
    old_id = _unknown(environment, original)
    successor = replace(original, provider_id=2, logical_request_id="next-candidate")
    new_id = ledger.start_exchange(environment.factory, successor, chain_id="next-chain", request_hash="b" * 64)
    ledger.receive_exchange(environment.factory, new_id, outcome="not_started")
    later = replace(original, purpose="group_semantic_review", route_set_id="group_semantic_review",
                    logical_request_id="review-stage")
    ledger.start_exchange(environment.factory, later, chain_id="review-chain", request_hash="c" * 64)
    rows = {row.id: row for row in _rows(environment)}
    assert rows[old_id].outcome == "unknown" and rows[old_id].local_termination_confirmed is True
    assert len(rows) == 3


@pytest.mark.parametrize("invalid", ("started", "response_received", "local_unproven", "same_candidate",
    "same_request", "disabled", "channel", "deadline", "route", "closed_config", "backwards"))
def test_unproved_or_unauthorized_successor_never_starts_http(environment, monkeypatch, invalid):
    _enable(environment, monkeypatch)
    original = _scope(environment)
    if invalid == "backwards":
        original = replace(original, provider_id=2)
    outcome = invalid if invalid in {"started", "response_received"} else "unknown"
    old_id = _unknown(environment, original, outcome=outcome, terminated=invalid != "local_unproven")
    successor = replace(original, provider_id=2, logical_request_id="next-candidate")
    if invalid in {"same_candidate", "backwards"}:
        successor = replace(successor, provider_id=1)
    elif invalid == "same_request":
        successor = replace(successor, logical_request_id=original.logical_request_id)
    elif invalid == "disabled":
        successor = replace(successor, emergency_enabled=False)
    elif invalid == "deadline":
        monkeypatch.setattr(failover, "_now", lambda: NOW + timedelta(minutes=2))
    elif invalid == "route":
        successor = replace(successor, model_name="not-approved")
    elif invalid in {"channel", "closed_config"}:
        with environment.factory() as session, session.begin():
            task = session.get(Task, "group_ai_chat")
            if invalid == "channel":
                task.type = "channel_comment"
            else:
                task.type_config = {**task.type_config, "emergency_fallback_enabled": False}
    with pytest.raises(AiProviderResultUnknown, match="previous_exchange_unresolved"):
        ledger.start_exchange(environment.factory, successor, chain_id="next-chain", request_hash="b" * 64)
    assert [(row.id, row.outcome) for row in _rows(environment)] == [(old_id, outcome)]


@pytest.mark.parametrize("kind", ("draft", "structured"))
def test_real_http_timeout_rotates_approved_candidate_and_retains_first_unknown(environment, monkeypatch, kind):
    config, provider = _enable(environment, monkeypatch)
    config = {**config, "_ai_execution_timing": {**config["_ai_execution_timing"], "llm_timeout_ceiling_seconds": 1}}
    module = drafts if kind == "draft" else structured
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setattr(module, "begin_provider_call", lambda *_: None)
    result_body = '{"drafts":[{"content":"QA 完整回复"}]}' if kind == "draft" else '{"ok":true}'
    response = json.dumps({"choices": [{"message": {"content": result_body}}]}).encode()
    with local_http_server(response_body=lambda _: response) as (url, observed), environment.factory() as session:
        providers = [environment.provider, provider]
        calls = [(item, AiProviderCredentials("QA", "openai_compatible", url + suffix, "mimo-v2.5", "QA"))
                 for item, suffix in zip(providers, ("/stall", "/ok"))]
        monkeypatch.setattr(module, "draft_provider_calls" if kind == "draft" else "structured_provider_calls", lambda *_: (providers, iter(calls)))
        result = _generate(kind, session, environment.provider, config)
        assert result is not None and len(observed) == 2
        assert [row.outcome for row in session.scalars(select(AiProviderAttempt).order_by(AiProviderAttempt.attempt_index))] == ["provider_result_unknown", "success"]
    rows = _rows(environment)
    assert {row.outcome for row in rows} == {"unknown", "settled"}
    assert next(row for row in rows if row.outcome == "unknown").local_termination_confirmed is True


def _generate(kind, session, provider, config):
    if kind == "draft":
        request = drafts.ProviderDraftRequest("json drafts", 1, "QA", "QA", ("自动化助手",), 0.7, 512, "QA", 1, "draft-logical")
        policy = drafts.ProviderCandidatePolicy("mimo-v2.5", "", False, PURPOSE, True,
            route_provider_ids=(1, 2), route_models={1: "mimo-v2.5", 2: "mimo-v2.5"}, attempt_config=config)
        return drafts.generate_with_provider_candidates(session, provider, request, policy=policy)
    request = structured.StructuredProviderRequest("QA", "QA", config, 0.7, 512, 1, PURPOSE, "mimo-v2.5", "primary", "")
    return structured.generate_structured_with_candidates(session, provider, request)


@pytest.mark.parametrize("kind", ("draft", "structured"))
def test_final_other_failure_does_not_hide_earlier_unknown(kind):
    unknown = AiProviderResultUnknown("first candidate unknown")
    failure_type = drafts._CandidateFailures if kind == "draft" else structured._StructuredFailures
    outcome = drafts.DraftAttemptOutcome if kind == "draft" else structured.StructuredAttemptOutcome
    failures = failure_type().add(outcome(None, unknown, False, True)).add(outcome(None, ValueError("later error"), False, False))
    with pytest.raises(AiProviderResultUnknown) as caught:
        failures.raise_final(None, 2)
    assert caught.value is unknown
