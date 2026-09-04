from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.ai_transport_errors import AiProviderResultUnknown
from app.models import GenerationJob
from app.services.task_center import provider_http_exchanges as ledger
from app.services.task_center.comment_generation_job import invalidate_comment_generation_jobs
from tests.test_generation_timing_binding import NOW, _bind, _job
from tests.test_provider_http_exchanges import environment, _request, _rows, _tracker


pytestmark = pytest.mark.no_postgres


def _successor(environment, *, epoch_change=False):
    with environment.factory() as session:
        old = session.get(GenerationJob, environment.jobs[0])
        old.state = "failed"  # Reproduce an old invalidation path or imported historical state.
        session.flush()
        task, job = _job(session, identity=str(uuid4()))
        if epoch_change:
            task.task_lifecycle_epoch += 1
        job.task_lifecycle_epoch = task.task_lifecycle_epoch
        job.obligation_type, job.obligation_id = old.obligation_type, old.obligation_id
        job.generation_sequence = old.generation_sequence + 1
        job.state, job.generation_owner_id, job.generation_lease_epoch = "generating", "QA-worker", 8
        job.lease_expires_at = NOW + timedelta(minutes=5)
        snapshot = _bind(session, task, job)["bindings"]
        session.commit()
    return tuple(snapshot)


@pytest.mark.parametrize("outcome", ("started", "response_received", "unknown"))
@pytest.mark.parametrize("epoch_change", (False, True))
def test_successor_cannot_escape_old_unresolved_exchange(environment, outcome, epoch_change):
    old_tracker = _tracker(environment)
    exchange_id = ledger.start_exchange(environment.factory, old_tracker.scope,
        chain_id=old_tracker.chain_id, request_hash="a" * 64)
    if outcome != "started":
        ledger.receive_exchange(environment.factory, exchange_id, outcome=outcome)
    snapshots = _successor(environment, epoch_change=epoch_change)
    calls = []
    successor = replace(_tracker(environment, transport=lambda *args, **kwargs: calls.append(True)),
        scope=replace(old_tracker.scope, job_bindings=snapshots), chain_id="successor")
    with pytest.raises(AiProviderResultUnknown, match="previous_exchange_unresolved"):
        _request(successor)
    assert calls == []
    assert [(row.id, row.outcome) for row in _rows(environment)] == [(exchange_id, outcome)]


def test_same_chain_name_does_not_authorize_a_different_generation_job(environment):
    old = _tracker(environment, transport=lambda *args, **kwargs: b"QA")
    _request(old)
    successor = replace(old, scope=replace(old.scope, job_bindings=_successor(environment)))
    with pytest.raises(AiProviderResultUnknown):
        _request(successor)


def test_settled_lineage_allows_legal_new_generation(environment):
    old = _tracker(environment, transport=lambda *args, **kwargs: b"QA")
    _request(old)
    with environment.factory() as session:
        ledger.settle_provider_exchanges(session, environment.config, provider_id=1,
            request_id="QA-logical", outcome="success", chain_id=old.chain_id)
        session.commit()
    successor = replace(old, scope=replace(old.scope, job_bindings=_successor(environment)), chain_id="successor")
    assert _request(successor) == b"QA"
    assert len(_rows(environment)) == 2


def test_other_obligation_remains_available(environment):
    original = _tracker(environment, transport=lambda *args, **kwargs: b"QA")
    first_scope = replace(original.scope, job_bindings=original.scope.job_bindings[:1])
    ledger.start_exchange(environment.factory, first_scope, chain_id="old", request_hash="a" * 64)
    sibling = replace(original, scope=replace(original.scope, job_bindings=original.scope.job_bindings[1:]))
    assert _request(sibling) == b"QA"


@pytest.mark.parametrize("outcome", ("started", "response_received", "unknown"))
def test_comment_invalidation_retains_unresolved_physical_evidence(environment, outcome):
    tracker = _tracker(environment)
    with environment.factory() as session:
        job = session.get(GenerationJob, environment.jobs[0])
        job.obligation_type = "post_comment"
        session.commit()
    exchange_id = ledger.start_exchange(environment.factory, tracker.scope,
        chain_id=tracker.chain_id, request_hash="a" * 64)
    if outcome != "started":
        ledger.receive_exchange(environment.factory, exchange_id, outcome=outcome)
    with environment.factory() as session:
        job = session.get(GenerationJob, environment.jobs[0])
        action = SimpleNamespace(id="QA-action", tenant_id=job.tenant_id, task_id=job.task_id)
        payload = SimpleNamespace(comment_fulfillment_obligation_id=job.obligation_id)
        invalidate_comment_generation_jobs(session, action, payload, reason="source_edited")
        session.commit()
        assert job.state == "unknown" and job.evaluator_evidence["invalidation_reason"] == "source_edited"
    assert _rows(environment)[0].outcome == outcome
