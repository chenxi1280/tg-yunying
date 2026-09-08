"""Retention must not erase query dependencies or authoritative business owners."""
from datetime import timedelta
from uuid import uuid4

import pytest

from app.models import (
    Action, ExecutionAttempt, FulfillmentFactProjectionState,
    FulfillmentObligationProjection, ReactionFulfillmentObligation,
)
from app.services.task_center import recent_success as recent
from app.services.task_center.runtime_retention import cleanup_runtime_details
from app.services.task_center.runtime_storage_maintenance import preview_runtime_details
from tests.test_recent_task_success import NOW, _record, _task, database, session  # noqa: F401


pytestmark = pytest.mark.no_postgres
OLD_ACTION_AGE = timedelta(days=10)


def _old_record(session, task, *, when):
    action, attempt, fact = _record(session, task, when=when)
    action.executed_at = NOW - OLD_ACTION_AGE
    session.flush()
    return action, attempt, fact


@pytest.mark.parametrize("task_type", tuple(recent.SUCCESS_KINDS))
def test_cleanup_preserves_success_counts_and_account_attribution_for_66_hour_fact(session, task_type):
    task = _task(session, task_type=task_type)
    action, attempt, _ = _old_record(session, task, when=NOW - timedelta(hours=66))
    before = recent.recent_task_success(session, task, now_value=NOW)
    preview = preview_runtime_details(session, as_of=NOW)
    assert action.id not in preview["candidate_ids"]
    assert cleanup_runtime_details(session, as_of=NOW) == 0
    session.expire_all()
    assert session.get(Action, action.id) is not None
    assert session.get(ExecutionAttempt, attempt.id) is not None
    assert recent.recent_task_success(session, task, now_value=NOW) == before


@pytest.mark.parametrize(("hours", "protected"), ((72, True), (73, False)))
def test_cleanup_uses_rolling_confirmation_boundary(session, hours, protected):
    task = _task(session)
    action, _, _ = _old_record(session, task, when=NOW - timedelta(hours=hours))
    preview = preview_runtime_details(session, as_of=NOW)
    assert (action.id not in preview["candidate_ids"]) is protected


def test_old_duplicate_fact_does_not_extend_dependency_retention(session):
    task = _task(session)
    action, _, _ = _old_record(session, task, when=NOW - timedelta(days=4))
    _record(session, task, action=action, when=NOW - timedelta(hours=1))
    assert recent.recent_task_success(session, task, now_value=NOW)["success_count"] == 0
    assert action.id in preview_runtime_details(session, as_of=NOW)["candidate_ids"]


def test_success_dependency_survives_later_skipped_action_status(session):
    task = _task(session)
    action, _, _ = _old_record(session, task, when=NOW - timedelta(hours=66))
    action.status = "skipped"
    session.flush()
    assert action.id not in preview_runtime_details(session, as_of=NOW)["candidate_ids"]


@pytest.mark.parametrize("invalid", ("wrong_tenant", "failed", "empty_message"))
def test_invalid_success_fact_does_not_retain_unrelated_details(session, invalid):
    task = _task(session)
    action, _, fact = _old_record(session, task, when=NOW - timedelta(hours=1))
    if invalid == "wrong_tenant":
        fact.tenant_id = 2
    elif invalid == "failed":
        fact.outcome = {**fact.outcome, "attempt_status": "failed"}
    else:
        fact.outcome = {**fact.outcome, "remote_message_id": ""}
    session.flush()
    assert action.id in preview_runtime_details(session, as_of=NOW)["candidate_ids"]


def test_unknown_fact_has_no_ttl_even_if_action_and_attempt_look_terminal(session):
    task = _task(session)
    action, _, fact = _old_record(session, task, when=NOW - timedelta(days=4))
    fact.fact_kind = "remote_outcome_unknown"
    session.flush()
    assert action.id not in preview_runtime_details(session, as_of=NOW)["candidate_ids"]


def test_business_obligation_is_not_a_disposable_action_detail(session):
    task = _task(session, task_type="channel_like")
    action, _, _ = _old_record(session, task, when=NOW - timedelta(days=4))
    # Foreign-key deletion behavior is exercised separately on real PostgreSQL.
    obligation = ReactionFulfillmentObligation(id=str(uuid4()), tenant_id=1,
        task_id=task.id, channel_message_id=101, account_id=11,
        reaction_contract_version=1, current_action_id=action.id, status="confirmed")
    session.add(obligation)
    session.flush()
    preview = preview_runtime_details(session, as_of=NOW)
    assert action.id not in preview["candidate_ids"]
    assert cleanup_runtime_details(session, as_of=NOW) == 0
    assert session.get(ReactionFulfillmentObligation, obligation.id) is obligation


@pytest.mark.parametrize("state", ("open", "remote_reconcile_only"))
def test_active_projection_without_fk_keeps_original_action(session, state):
    task = _task(session)
    action, _, _ = _old_record(session, task, when=NOW - timedelta(days=4))
    session.add(FulfillmentObligationProjection(tenant_id=1, task_id=task.id,
        obligation_type="group_slot", obligation_id=action.id, work_lane="group_direct",
        state=state, active_action_id=action.id))
    session.flush()
    assert cleanup_runtime_details(session, as_of=NOW) == 0


@pytest.mark.parametrize("state", ("pending", "failed"))
def test_unprojected_fact_keeps_its_dependencies(session, state):
    task = _task(session)
    action, _, fact = _old_record(session, task, when=NOW - timedelta(days=4))
    session.add(FulfillmentFactProjectionState(fact_id=fact.fact_id,
        projection_kind="confirmed_quantity", state=state))
    session.flush()
    assert action.id not in preview_runtime_details(session, as_of=NOW)["candidate_ids"]


def test_protected_oldest_row_does_not_starve_next_safe_batch(session):
    task = _task(session)
    protected, _, fact = _old_record(session, task, when=NOW - timedelta(days=4))
    fact.fact_kind = "remote_outcome_unknown"
    safe, _, _ = _old_record(session, task, when=NOW - timedelta(days=4))
    safe.executed_at += timedelta(hours=1)
    session.flush()
    assert preview_runtime_details(session, as_of=NOW, batch_size=1)["candidate_ids"] == [safe.id]
    assert cleanup_runtime_details(session, as_of=NOW, batch_size=1) == 2  # One Action and its Attempt.
    assert session.get(Action, protected.id) is protected
