"""A rejected deadline slot cannot erase another committed reservation."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import ExecutionAttempt, OperationTarget, SourcePacingAdmission, SourcePacingState, Tenant, TgAccount
from app.services.task_center.source_pacing import wall_datetime
from app.services.task_center.source_pacing_admission import admit_source_paced_attempt
from tests.postgres_pacing_e4_fixture import factory
from tests.test_source_pacing_admission import NOW, _reaction_source_entities, _reaction_action_and_reservation

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def _seed(session):
    session.add(Tenant(id=1, name="neutral source deadline"))
    session.flush()
    session.add(TgAccount(id=1, tenant_id=1, display_name="neutral account", phone_masked="***"))
    session.add(OperationTarget(id=10, tenant_id=1, target_type="channel",
                                tg_peer_id="-1009001", title="neutral channel"))
    session.flush()
    message, task, obligation = _reaction_source_entities()
    action, reservation, deadline = _reaction_action_and_reservation(task, obligation)
    action.status = "retryable_failed"
    for row in (message, task, obligation, action, reservation):
        session.add(row)
        session.flush()
    attempt = ExecutionAttempt(tenant_id=1, action_id=action.id, account_id=1,
                               attempt_no=1, status="before_call")
    session.add(attempt)
    session.flush()
    return action, attempt, deadline


def test_deadline_rejection_retains_row_lock_and_no_call_fact(factory):
    with factory() as owner:
        action, attempt, deadline = _seed(owner)
        owner.commit()
        assert not admit_source_paced_attempt(owner, action, attempt, now_value=deadline)
        state = owner.scalar(select(SourcePacingState))
        with factory() as observer:
            # New state rows are uncommitted; nobody can observe a usable slot.
            assert observer.scalar(select(SourcePacingState.id).with_for_update(skip_locked=True)) is None
        owner.commit()
    with factory() as readback:
        state = readback.scalar(select(SourcePacingState))
        admission = readback.scalar(select(SourcePacingAdmission))
        attempt = readback.scalar(select(ExecutionAttempt))
        assert state.next_call_not_before_at is None
        assert state.last_call_started_at is None
        assert admission.state == "cancelled_pre_gateway"
        assert attempt.gateway_call_started_at is None
        assert attempt.failure_type == "pacing_source_period_exhausted"


def test_expired_existing_slot_releases_only_its_own_tail(factory):
    with factory() as owner:
        action, attempt, deadline = _seed(owner)
        action.effective_claim_at = NOW + timedelta(seconds=30)
        assert not admit_source_paced_attempt(owner, action, attempt, now_value=NOW)
        state = owner.scalar(select(SourcePacingState))
        own = owner.scalar(select(SourcePacingAdmission))
        valid_at = deadline + timedelta(hours=1)
        other = SourcePacingAdmission(tenant_id=1, task_id=action.task_id,
            source_pacing_state_id=state.id, admission_key="neutral-other",
            owner_type=own.owner_type, owner_id="neutral-other-owner",
            pacing_period_key="neutral-other-period", pacing_plan_hash="b" * 64,
            planned_release_at=valid_at, call_not_before_at=valid_at, source_gap_seconds=60,
            state="reserved")
        owner.add(other)
        state.next_call_not_before_at = valid_at + timedelta(seconds=60)
        owner.commit()
        retry = ExecutionAttempt(tenant_id=1, action_id=action.id, account_id=1,
                                 attempt_no=2, status="before_call")
        owner.add(retry)
        owner.flush()
        assert not admit_source_paced_attempt(owner, action, retry, now_value=deadline)
        with factory() as observer:
            assert observer.scalar(select(SourcePacingState.id).where(
                SourcePacingState.id == state.id).with_for_update(skip_locked=True)) is None
        owner.commit()
    with factory() as readback:
        state = readback.scalar(select(SourcePacingState))
        assert wall_datetime(state.next_call_not_before_at) == valid_at + timedelta(seconds=60)
        other = readback.scalar(select(SourcePacingAdmission).where(
            SourcePacingAdmission.admission_key == "neutral-other"))
        assert other.state == "reserved"
