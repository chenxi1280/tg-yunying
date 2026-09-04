from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.ai_transport_errors import AiProviderResultUnknown
from app.database import Base
from app.models import Action, GenerationJob
from app.services._common import _now
from app.services.task_center import ai_generation_worker
from app.services.task_center.ai_generation_parallel_settlement import settle_parallel_outcome, settle_sequential_outcome
from app.services.task_center.ai_generation_state import GenerationAttemptStale
from app.services.task_center.ai_generation_worker_types import GenerationOutcome, SequentialClaim
from app.services.task_center.ai_generator import AiGenerationUnavailable
from app.services.task_center.generation_provider_unknown import persist_group_provider_unknown
from tests.ai_generation_phase_test_support import seed_reserved_normal_batch
from tests.test_ai_generation_failure_state import _action_session, _dependencies


pytestmark = pytest.mark.no_postgres


def test_actual_worker_preserves_provider_unknown_and_does_not_reclaim(monkeypatch):
    calls = []

    def unknown(*args, **kwargs):
        calls.append(1)
        raise AiProviderResultUnknown("QA transport deadline")

    with _action_session() as (session, action):
        monkeypatch.setattr(ai_generation_worker, "credentials_for_account", lambda *args: object())
        factory = lambda: Session(session.get_bind(), autoflush=False)
        assert ai_generation_worker.drain_ai_generation(factory, limit=1, dependencies=_dependencies(unknown)) == 1
        session.refresh(action)
        assert action.status == "pending"
        assert action.payload["ai_generation_status"] == "provider_result_unknown"
        assert action.result["generation_outcome"] == "provider_result_unknown"
        assert not action.claim_owner and not action.lease_owner
        assert ai_generation_worker.drain_ai_generation(factory, limit=1, dependencies=_dependencies(unknown)) == 0
        assert calls == [1]


def _seed(session):
    actions, coverages = seed_reserved_normal_batch(session, _now())
    for index, action in enumerate(actions):
        job = GenerationJob(id=f"job-{index}", tenant_id=action.tenant_id, task_id=action.task_id,
            task_lifecycle_epoch=action.task_lifecycle_epoch, obligation_type=f"qa-{index}", obligation_id=action.id,
            generation_sequence=1, context_snapshot_version=1, state="generating", generation_owner_id="worker-a")
        action.obligation_type, action.obligation_id = job.obligation_type, job.obligation_id
        action.payload = {**action.payload, "generation_job_id": job.id, "ai_generation_attempt_id": "attempt-qa"}
        session.add(job)
    session.commit()
    request = SimpleNamespace(batch_ids=[a.id for a in actions], tenant_id=1, task_id=actions[0].task_id,
                              claim_owner="worker-a", claim_token="claim-normal", attempt_id="attempt-qa", group_id=7)
    return actions, coverages, request


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as current:
        yield current
    engine.dispose()


def test_batch_unknown_preserves_quantity_ownership_and_finishes_jobs_atomically(session):
    actions, coverages, request = _seed(session)
    persist_group_provider_unknown(session, request, detail="QA unknown")
    session.commit()
    jobs = list(session.scalars(select(GenerationJob)))
    assert all(job.state == "unknown" and job.generation_stage == "provider_result_unknown" for job in jobs)
    assert all(not job.generation_owner_id and job.next_retry_at is None for job in jobs)
    assert all(action.payload["ai_generation_status"] == "provider_result_unknown" for action in actions)
    assert all(coverage.state == "reserved" and coverage.reserved_action_id == action.id for coverage, action in zip(coverages, actions))
    assert all(not action.payload["ai_generation_result_cache"] for action in actions)
    factory = lambda: Session(session.get_bind(), autoflush=False)
    failure = GenerationOutcome(failure=AiGenerationUnavailable("provider_result_unknown"))
    claim = SimpleNamespace(action_id=actions[0].id, owner=request.claim_owner, token=request.claim_token, job_id=jobs[0].id)
    assert settle_parallel_outcome(factory, claim, failure) == 1
    sequential = SequentialClaim(actions[0].id, request.claim_owner, request.claim_token, len(actions))
    assert settle_sequential_outcome(factory, sequential, failure) == 2
    session.expire_all()
    assert all(job.state == "unknown" for job in jobs)


@pytest.mark.parametrize("changed", ("claim", "job_scope"))
def test_stale_or_wrong_scope_unknown_cannot_overwrite_new_owner(session, changed):
    actions, _, request = _seed(session)
    if changed == "claim":
        actions[1].payload = {**actions[1].payload, "ai_generation_claim_token": "new-token"}
    else:
        session.get(GenerationJob, "job-1").task_lifecycle_epoch += 1
    session.commit()
    with pytest.raises(GenerationAttemptStale):
        persist_group_provider_unknown(session, request, detail="stale QA")
    session.rollback()
    assert all(action.status == "executing" for action in actions)
    assert all(job.state == "generating" for job in session.scalars(select(GenerationJob)))


def test_parallel_worker_keeps_unknown_jobs_instead_of_finishing_them_failed(session, monkeypatch):
    from app.models import Task
    from app.services.task_center import ai_generation_dispatch
    from app.services.task_center.ai_generation_parallel import ParallelGenerationClaim, claim_parallel_generation

    actions, _, request = _seed(session)
    session.get(Task, request.task_id).status = "running"
    session.commit()
    request.is_reply, request.cached_contents = False, []

    def unknown(*args):
        raise AiProviderResultUnknown("QA ambiguous completion")

    monkeypatch.setattr(ai_generation_dispatch, "generate_quality_results", unknown)

    def processor(current, action, account):
        ai_generation_dispatch._generate_without_transaction(current, request, None)

    factory = lambda: Session(session.get_bind(), autoflush=False)
    claim = ParallelGenerationClaim(actions[0].id, "job-0", request.claim_owner, request.claim_token, 1, 0)
    assert ai_generation_worker._process_parallel_claim(factory, processor, claim) == 1
    session.expire_all()
    assert all(job.state == "unknown" for job in session.scalars(select(GenerationJob)))
    assert claim_parallel_generation(factory, owner="new-worker", limit=2) == ()
