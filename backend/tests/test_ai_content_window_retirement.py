import pytest
from sqlalchemy.orm import Session

from app.models import AiContentWindowPlan, ExecutionAttempt, FulfillmentRemoteFact
from app.services._common import _now
from app.services.task_center.ai_content_runtime import invalidate_terminal_pre_gateway_obligation_slot
from tests.test_ai_generation_legacy_reconcile import _owned_slot
from tests.test_ai_generation_reconcile_fencing import _action, _engine, _job, _seed_scope


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def state():
    with Session(_engine()) as session:
        _seed_scope(session)
        job = _job("job-old", "obligation", state="ready")
        job.generation_stage = "gateway_bound"
        action = _action("action-old", job.obligation_id, job.id,
                         status="failed", generation_status="ready")
        action.result = {"error_code": "context_freshness_unproven"}
        plan = AiContentWindowPlan(id="plan-old", tenant_id=1, task_id=job.task_id,
            task_lifecycle_epoch=1, scope_type="group", scope_id="7",
            pacing_plan_hash="p" * 64, period_key="old-window", window_start_at=_now(),
            window_end_at=_now(), task_config_revision=1, content_policy_hash="c" * 64,
            plan_hash="h" * 64, state="frozen")
        session.add_all([job, action, plan])
        session.flush()
        slot = _owned_slot("slot-old", plan.id, job, state="gateway_bound", ordinal=1)
        session.add(slot)
        session.flush()
        job.window_slot_id = slot.id
        session.commit()
        yield session, action, job, slot


def _retire(session, job):
    return invalidate_terminal_pre_gateway_obligation_slot(session,
        obligation_type=job.obligation_type, obligation_id=job.obligation_id)


def test_terminal_ready_action_without_attempt_releases_only_window(state):
    session, action, job, slot = state
    versions = action.action_version, job.job_version, slot.version
    assert _retire(session, job)
    session.flush()
    assert slot.state == "invalidated" and slot.claimed_by_job_id is None
    assert slot.version == versions[2] + 1 and slot.lease_expires_at is None
    assert (action.status, action.action_version) == ("failed", versions[0])
    assert (job.state, job.job_version, job.window_slot_id) == ("ready", versions[1], slot.id)


@pytest.mark.parametrize("case", ["action_unknown", "job_unknown", "active", "epoch", "job_id", "attempt", "called", "fact"])
def test_unproven_or_wrong_owner_keeps_window(state, case):
    session, action, job, slot = state
    if case in {"action_unknown", "active"}:
        action.status = "unknown_after_send" if case == "action_unknown" else "pending"
    if case == "job_unknown":
        job.state = "unknown"
    if case == "epoch":
        action.task_lifecycle_epoch += 1
    if case == "job_id":
        action.payload = {**action.payload, "generation_job_id": "different-job"}
    if case in {"attempt", "called"}:
        session.add(ExecutionAttempt(action_id=action.id, tenant_id=1,
            gateway_call_started_at=_now() if case == "called" else None))
    if case == "fact":
        session.add(FulfillmentRemoteFact(tenant_id=1, task_id=action.task_id,
            task_type="group_ai_chat", action_id=action.id, obligation_type=job.obligation_type,
            obligation_id=job.obligation_id, fact_kind="remote_outcome_unknown",
            attempt_id="historical-attempt", mutation_kind="send_message",
            remote_mutation_key_hash="m" * 64, gateway_request_hash="g" * 64,
            fact_identity_hash="f" * 64, observed_at=_now()))
    session.commit()
    version = slot.version
    assert not _retire(session, job)
    assert (slot.state, slot.claimed_by_job_id, slot.version) == ("gateway_bound", job.id, version)


def _failed_attempt_with_fact(session, action, job):
    attempt = ExecutionAttempt(id="failed-attempt", tenant_id=action.tenant_id, action_id=action.id,
        task_lifecycle_epoch=action.task_lifecycle_epoch, status="failed",
        gateway_call_started_at=_now(), after_call_at=_now())
    fact = FulfillmentRemoteFact(tenant_id=action.tenant_id, task_id=action.task_id,
        task_type=action.task_type, action_id=action.id, obligation_type=job.obligation_type,
        obligation_id=job.obligation_id, fact_kind="safely_not_executed", attempt_id=attempt.id,
        mutation_kind=action.action_type, remote_mutation_key_hash="m" * 64,
        gateway_request_hash="g" * 64, fact_identity_hash="f" * 64, observed_at=_now())
    session.add_all([attempt, fact])
    return attempt, fact


@pytest.mark.parametrize("called", [False, True])
def test_terminal_nonexecution_evidence_releases_only_window(state, called):
    session, action, job, slot = state
    if called:
        _failed_attempt_with_fact(session, action, job)
    else:
        session.add(ExecutionAttempt(action_id=action.id, tenant_id=action.tenant_id,
            task_lifecycle_epoch=action.task_lifecycle_epoch, status="skipped_before_gateway", after_call_at=_now()))
    session.commit()
    assert _retire(session, job)
    assert slot.state == "invalidated"
    assert (action.status, job.state) == ("failed", "ready")


@pytest.mark.parametrize("case", ["tenant", "task", "obligation", "mutation", "orphan", "unknown_fact",
                                  "unfinished", "unknown_attempt", "remote_id", "another_attempt"])
def test_incomplete_or_mismatched_nonexecution_evidence_preserves_window(state, case):
    session, action, job, slot = state
    attempt, fact = _failed_attempt_with_fact(session, action, job)
    mismatches = {"tenant": ("tenant_id", 2), "task": ("task_id", "another-task"),
        "obligation": ("obligation_id", "another-obligation"), "mutation": ("mutation_kind", "view_message"),
        "orphan": ("attempt_id", "another-attempt"), "unknown_fact": ("fact_kind", "remote_outcome_unknown")}
    if case in mismatches:
        key, value = mismatches[case]
        setattr(fact, key, value)
    if case == "unfinished":
        attempt.after_call_at = None
    if case == "unknown_attempt":
        attempt.status = "unknown_after_send"
    if case == "remote_id":
        attempt.remote_message_id = "remote-message"
    if case == "another_attempt":
        session.add(ExecutionAttempt(action_id=action.id, tenant_id=action.tenant_id, attempt_no=2))
    session.commit()
    assert not _retire(session, job)
    assert slot.state == "gateway_bound"
