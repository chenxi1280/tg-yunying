from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.no_postgres

from app.models import (
    AccountPacingReservation,
    Action,
    ChannelViewDailyIdentityOwner,
    ExecutionAttempt,
    FulfillmentRemoteFact,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)
from app.services.task_center.channel_fulfillment import (
    cancel_superseded_channel_actions,
    confirm_view_obligation,
)
from app.services.task_center.channel_view_daily_identity import (
    mark_daily_identity_call_issued,
)
from app.services.task_center.direct_action_claims import (
    settle_fact_first_action_before_gateway,
)
from app.services.task_center.executors.channel_view import build_plan
from app.services.task_center.fulfillment_remote_facts import ensure_action_obligation
from app.services.task_center import dispatcher
from app.services.task_center.service import delete_task, stop_task
from app.timezone import BEIJING_TZ
from tests.channel_view_coverage_support import (
    add_message,
    add_view_task,
    new_session,
    seed_channel_scenario,
)


def _set_view_clock(monkeypatch, value: datetime) -> None:
    monkeypatch.setattr("app.services.task_center.executors.channel_view._now", lambda: value)
    monkeypatch.setattr("app.services.task_center.daily_ledgers._now", lambda: value)


def _seed_two_tasks(session, *, channel_id: int, current: datetime) -> tuple[Task, Task]:
    scenario = seed_channel_scenario(session, channel_id=channel_id, account_count=1)
    message = add_message(
        session,
        channel=scenario.channel,
        message_id=channel_id,
        published_at=current - timedelta(hours=1),
    )
    tasks = tuple(
        add_view_task(
            session,
            channel=scenario.channel,
            messages=[message],
            task_id=f"daily-owner-{channel_id}-{suffix}",
            daily_target=1,
            total_target=0,
        )
        for suffix in ("one", "two")
    )
    return tasks


def _view_obligation(session, task: Task) -> ViewFulfillmentObligation:
    obligation = session.scalar(
        select(ViewFulfillmentObligation)
        .join(TaskDayLedger)
        .where(TaskDayLedger.task_id == task.id)
    )
    assert obligation is not None
    return obligation


def _bind_pacing_reservation(
    session,
    action: Action,
    *,
    current: datetime,
) -> AccountPacingReservation:
    action.pacing_slot_key = f"lifecycle:{action.id}"
    reservation = AccountPacingReservation(
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        account_id=action.account_id,
        pacing_slot_key=action.pacing_slot_key,
        policy_version="account_soft_pacing_v1",
        due_at=current,
        release_not_before_at=current,
        effective_claim_at=current,
        source_deadline_at=current + timedelta(hours=1),
        action_id=action.id,
        state="bound",
    )
    session.add(reservation)
    session.flush()
    return reservation


def _assert_safe_fact(session, action: Action) -> None:
    attempt = session.scalar(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id == action.id,
    ))
    fact = session.scalar(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.action_id == action.id,
    ))
    assert attempt is not None and attempt.gateway_call_started_at is None
    assert fact is not None and fact.fact_kind == "safely_not_executed"


def test_view_remote_fact_navigation_is_nullable_set_null():
    column = ViewRemoteFact.__table__.c.obligation_id
    foreign_key = next(iter(column.foreign_keys))

    assert column.nullable is True
    assert foreign_key.ondelete == "SET NULL"
    assert foreign_key.constraint.name == "fk_view_remote_fact_obligation_navigation"


def test_view_fact_survives_obligation_deletion_with_null_navigation(monkeypatch):
    current = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, current)
    with new_session() as session:
        session.commit()
        session.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
        first, _second = _seed_two_tasks(session, channel_id=77, current=current)
        assert build_plan(session, first) == 1
        action = session.scalar(select(Action).where(Action.task_id == first.id))
        obligation = _view_obligation(session, first)
        owner = session.scalar(select(ChannelViewDailyIdentityOwner))
        assert action is not None and owner is not None
        fact = confirm_view_obligation(
            session,
            obligation,
            target_peer_id=owner.target_peer_id,
            confirmed_at=current,
        )
        session.commit()

        session.delete(obligation)
        session.commit()

        session.refresh(fact)
        session.refresh(owner)
        assert fact.obligation_id is None
        assert owner.obligation_id is None
        assert fact.target_peer_id == owner.target_peer_id


def test_task_stop_releases_pre_gateway_daily_identity(monkeypatch):
    current = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, current)
    with new_session() as session:
        first, second = _seed_two_tasks(session, channel_id=78, current=current)
        assert build_plan(session, first) == 1

        stop_task(session, 1, first.id, "test")

        owner = session.scalar(select(ChannelViewDailyIdentityOwner))
        action = session.scalar(select(Action).where(Action.task_id == first.id))
        obligation = _view_obligation(session, first)
        assert owner is not None and owner.state == "available"
        assert owner.obligation_id is None and owner.action_id is None
        assert action is not None and action.status == "skipped"
        _assert_safe_fact(session, action)
        assert obligation.status == "open" and obligation.current_action_id is None
        assert build_plan(session, second) == 1


def test_task_stop_releases_retryable_failed_daily_identity(monkeypatch):
    current = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, current)
    with new_session() as session:
        first, second = _seed_two_tasks(session, channel_id=781, current=current)
        assert build_plan(session, first) == 1
        action = session.scalar(select(Action).where(Action.task_id == first.id))
        assert action is not None
        action.status = "retryable_failed"
        session.commit()

        stop_task(session, 1, first.id, "test")

        owner = session.scalar(select(ChannelViewDailyIdentityOwner))
        session.refresh(action)
        assert action.status == "skipped"
        assert owner is not None and owner.state == "available"
        assert owner.obligation_id is None and owner.action_id is None
        assert build_plan(session, second) == 1


def test_task_stop_releases_bound_pacing_reservation_once(monkeypatch):
    current = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, current)
    with new_session() as session:
        first, second = _seed_two_tasks(session, channel_id=782, current=current)
        assert build_plan(session, first) == 1
        action = session.scalar(select(Action).where(Action.task_id == first.id))
        assert action is not None
        reservation = _bind_pacing_reservation(session, action, current=current)
        session.commit()

        stop_task(session, 1, first.id, "test")

        session.refresh(reservation)
        assert reservation.state == "missed"
        assert build_plan(session, second) == 1


def test_task_delete_releases_pre_gateway_daily_identity(monkeypatch):
    current = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, current)
    with new_session() as session:
        first, second = _seed_two_tasks(session, channel_id=79, current=current)
        assert build_plan(session, first) == 1

        delete_task(session, 1, first.id, "test")

        owner = session.scalar(select(ChannelViewDailyIdentityOwner))
        action = session.scalar(select(Action).where(Action.task_id == first.id))
        assert owner is not None and owner.state == "available"
        assert action is not None
        _assert_safe_fact(session, action)
        assert owner.obligation_id is None and owner.action_id is None
        assert build_plan(session, second) == 1


def test_lifecycle_supersede_releases_pre_gateway_daily_identity(monkeypatch):
    current = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, current)
    with new_session() as session:
        first, second = _seed_two_tasks(session, channel_id=80, current=current)
        assert build_plan(session, first) == 1
        first.task_lifecycle_epoch += 1
        session.flush()

        assert cancel_superseded_channel_actions(session, first) == 1

        owner = session.scalar(select(ChannelViewDailyIdentityOwner))
        action = session.scalar(select(Action).where(Action.task_id == first.id))
        assert owner is not None and owner.state == "available"
        assert action is not None
        _assert_safe_fact(session, action)
        assert owner.obligation_id is None and owner.action_id is None
        assert build_plan(session, second) == 1


def test_lifecycle_supersede_releases_retryable_failed_daily_identity(monkeypatch):
    current = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, current)
    with new_session() as session:
        first, second = _seed_two_tasks(session, channel_id=801, current=current)
        assert build_plan(session, first) == 1
        action = session.scalar(select(Action).where(Action.task_id == first.id))
        assert action is not None
        action.status = "retryable_failed"
        first.task_lifecycle_epoch += 1
        session.flush()

        assert cancel_superseded_channel_actions(session, first) == 1

        owner = session.scalar(select(ChannelViewDailyIdentityOwner))
        assert owner is not None and owner.state == "available"
        assert build_plan(session, second) == 1


def test_lifecycle_supersede_releases_bound_pacing_reservation_once(monkeypatch):
    current = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, current)
    with new_session() as session:
        first, second = _seed_two_tasks(session, channel_id=802, current=current)
        assert build_plan(session, first) == 1
        action = session.scalar(select(Action).where(Action.task_id == first.id))
        assert action is not None
        reservation = _bind_pacing_reservation(session, action, current=current)
        first.task_lifecycle_epoch += 1
        session.flush()

        assert cancel_superseded_channel_actions(session, first) == 1

        session.refresh(reservation)
        assert reservation.state == "missed"
        assert build_plan(session, second) == 1


def test_task_stop_preserves_gateway_started_daily_identity(monkeypatch):
    current = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, current)
    with new_session() as session:
        first, second = _seed_two_tasks(session, channel_id=81, current=current)
        assert build_plan(session, first) == 1
        action = session.scalar(select(Action).where(Action.task_id == first.id))
        assert action is not None
        mark_daily_identity_call_issued(session, action)
        action.status = "executing"
        session.add(ExecutionAttempt(
            tenant_id=1,
            action_id=action.id,
            account_id=action.account_id,
            attempt_no=1,
            status="gateway_call_started",
            gateway_call_started_at=current,
            result_snapshot={"remote_mutation_started": True},
        ))
        session.commit()

        stop_task(session, 1, first.id, "test")

        owner = session.scalar(select(ChannelViewDailyIdentityOwner))
        obligation = _view_obligation(session, first)
        session.refresh(action)
        assert owner is not None and owner.state == "unknown"
        assert owner.obligation_id == obligation.id and owner.action_id == action.id
        assert obligation.status == "unknown" and obligation.current_action_id == action.id
        assert action.status == "unknown_after_send"
        assert build_plan(session, second) == 0


def test_task_stop_releases_call_issued_with_authoritative_no_mutation(monkeypatch):
    current = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, current)
    with new_session() as session:
        first, second = _seed_two_tasks(session, channel_id=82, current=current)
        assert build_plan(session, first) == 1
        action = session.scalar(select(Action).where(Action.task_id == first.id))
        assert action is not None
        mark_daily_identity_call_issued(session, action)
        action.status = "executing"
        session.add(ExecutionAttempt(
            tenant_id=1,
            action_id=action.id,
            account_id=action.account_id,
            attempt_no=1,
            status="failed",
            gateway_call_started_at=current,
            result_snapshot={"remote_mutation_started": False},
        ))
        session.commit()

        stop_task(session, 1, first.id, "test")

        owner = session.scalar(select(ChannelViewDailyIdentityOwner))
        obligation = _view_obligation(session, first)
        session.refresh(action)
        assert owner is not None and owner.state == "available"
        assert owner.obligation_id is None and owner.action_id is None
        assert obligation.status == "open" and obligation.current_action_id is None
        assert action.status == "skipped"
        assert build_plan(session, second) == 1


def test_task_stop_preserves_older_unsafe_attempt_over_latest_false(monkeypatch):
    current = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, current)
    with new_session() as session:
        first, second = _seed_two_tasks(session, channel_id=83, current=current)
        assert build_plan(session, first) == 1
        action = session.scalar(select(Action).where(Action.task_id == first.id))
        assert action is not None
        mark_daily_identity_call_issued(session, action)
        action.status = "executing"
        session.add_all([
            ExecutionAttempt(
                tenant_id=1,
                action_id=action.id,
                account_id=action.account_id,
                attempt_no=1,
                status="result_unknown",
                gateway_call_started_at=current,
                result_snapshot={"remote_mutation_started": True},
            ),
            ExecutionAttempt(
                tenant_id=1,
                action_id=action.id,
                account_id=action.account_id,
                attempt_no=2,
                status="failed",
                gateway_call_started_at=current + timedelta(seconds=1),
                result_snapshot={"remote_mutation_started": False},
            ),
        ])
        session.commit()

        stop_task(session, 1, first.id, "test")

        owner = session.scalar(select(ChannelViewDailyIdentityOwner))
        assert owner is not None and owner.state == "unknown"
        assert owner.action_id == action.id
        assert action.status == "unknown_after_send"
        assert build_plan(session, second) == 0
        action.status = "failed"
        with pytest.raises(
            RuntimeError, match="pre_gateway_safe_settlement_remote_evidence_unsafe:"
        ):
            settle_fact_first_action_before_gateway(
                session,
                action,
                now=current,
                reason_code="unsafe_replay",
                detail="unsafe replay",
            )


def test_finalizer_preserves_older_unsafe_attempt_over_latest_pre_gateway(monkeypatch):
    current = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, current)
    with new_session() as session:
        first, second = _seed_two_tasks(session, channel_id=84, current=current)
        assert build_plan(session, first) == 1
        action = session.scalar(select(Action).where(Action.task_id == first.id))
        assert action is not None
        mark_daily_identity_call_issued(session, action)
        action.status = "failed"
        action.result = {"success": False, "remote_mutation_started": False}
        assert ensure_action_obligation(session, action)
        session.add_all([
            ExecutionAttempt(
                tenant_id=1,
                action_id=action.id,
                account_id=action.account_id,
                attempt_no=1,
                status="result_unknown",
                gateway_call_started_at=current,
                result_snapshot={"remote_mutation_started": True},
            ),
            ExecutionAttempt(
                tenant_id=1,
                action_id=action.id,
                account_id=action.account_id,
                attempt_no=2,
                status="failed",
                result_snapshot={"remote_mutation_started": False},
            ),
        ])
        session.flush()

        dispatcher._finalize_fact_first_dispatch(session, action)

        owner = session.scalar(select(ChannelViewDailyIdentityOwner))
        fact = session.scalar(select(FulfillmentRemoteFact).where(
            FulfillmentRemoteFact.action_id == action.id,
        ))
        assert owner is not None and owner.state == "unknown"
        assert owner.action_id == action.id
        assert fact is not None and fact.fact_kind == "remote_outcome_unknown"
        assert build_plan(session, second) == 0


def test_safe_settlement_replay_reads_committed_result(monkeypatch):
    current = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
    _set_view_clock(monkeypatch, current)
    with new_session() as session:
        first, _second = _seed_two_tasks(session, channel_id=85, current=current)
        assert build_plan(session, first) == 1
        action = session.scalar(select(Action).where(Action.task_id == first.id))
        assert action is not None
        reservation = _bind_pacing_reservation(session, action, current=current)
        stop_task(session, 1, first.id, "test")
        attempts_before = list(session.scalars(select(ExecutionAttempt).where(
            ExecutionAttempt.action_id == action.id,
        )))
        facts_before = list(session.scalars(select(FulfillmentRemoteFact).where(
            FulfillmentRemoteFact.action_id == action.id,
        )))

        state_ids = settle_fact_first_action_before_gateway(
            session,
            action,
            now=current,
            reason_code="task_stopped",
            detail="任务已停止",
        )
        session.commit()

        assert state_ids == set()
        assert len(list(session.scalars(select(ExecutionAttempt).where(
            ExecutionAttempt.action_id == action.id,
        )))) == len(attempts_before)
        assert len(list(session.scalars(select(FulfillmentRemoteFact).where(
            FulfillmentRemoteFact.action_id == action.id,
        )))) == len(facts_before)
        assert reservation.state == "missed"
