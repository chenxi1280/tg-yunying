from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import Action
from app.services.task_center import account_pacing_guard
from app.services.task_center.account_pacing_guard import (
    AccountPacingDeadlineExceeded,
    bind_account_pacing_reservation,
    reserve_account_pacing,
    revalidate_action_pacing_before_claim,
)
from app.services.task_center.engagement_behavior_sessions import ensure_behavior_session_plan
from test_engagement_behavior_sessions import _seed, _session


pytestmark = pytest.mark.no_postgres
DAY = datetime(2026, 9, 9)
FIRST_START = DAY.replace(hour=10)
FIRST_END = FIRST_START + timedelta(minutes=15)
NEXT_START = DAY.replace(hour=11)
DEADLINE = NEXT_START + timedelta(minutes=30)
PAIR_GAP = timedelta(minutes=5)


def _setup(session):
    task, _ = _seed(session)
    plan = ensure_behavior_session_plan(
        session, tenant_id=1, account_id=11, task_day=DAY.date(),
    )
    plan.windows = [
        {"start_at": FIRST_START.isoformat(), "end_at": FIRST_END.isoformat()},
        {"start_at": NEXT_START.isoformat(), "end_at": DEADLINE.isoformat()},
    ]
    session.flush()
    return task


def _block(session, task, *, at, status="pending"):
    action = Action(
        tenant_id=1, task_id=task.id, task_type=task.type,
        action_type="send_message", account_id=11, status=status,
        scheduled_at=at,
    )
    session.add(action)
    session.flush()
    return action


def _reserve(session, task, *, due, deadline=DEADLINE, key="window:mine"):
    return reserve_account_pacing(
        session, tenant_id=1, task_id=task.id, account_id=11,
        slot_key=key, due_at=due, deadline_at=deadline,
        engagement_contract_version="unified_engagement_v1",
        action_class="authored_message",
    )


@pytest.mark.parametrize("next_window_busy", [False, True])
def test_reserve_rechecks_window_after_account_timeline(next_window_busy):
    with _session() as session:
        task = _setup(session)
        _block(session, task, at=FIRST_END - timedelta(minutes=2))
        if next_window_busy:
            _block(session, task, at=NEXT_START)
        due = FIRST_END - timedelta(minutes=1)
        reservation = _reserve(session, task, due=due)

        expected = NEXT_START + PAIR_GAP if next_window_busy else NEXT_START
        assert reservation.effective_claim_at == expected
        assert reservation.release_not_before_at == NEXT_START
        assert reservation.due_at == due


def test_window_end_is_exclusive_after_timeline_shift():
    with _session() as session:
        task = _setup(session)
        _block(session, task, at=FIRST_END - PAIR_GAP)
        reservation = _reserve(session, task, due=FIRST_END - timedelta(seconds=1))

        assert reservation.effective_claim_at == NEXT_START


def test_no_intersection_before_deadline_is_not_a_valid_reservation():
    with _session() as session:
        task = _setup(session)
        _block(session, task, at=FIRST_END - timedelta(minutes=2))
        with pytest.raises(AccountPacingDeadlineExceeded, match="account_behavior_session_unavailable"):
            _reserve(
                session, task, due=FIRST_END - timedelta(minutes=1),
                deadline=FIRST_END + timedelta(minutes=15),
            )


def test_unbound_reservation_rearm_uses_window_intersection():
    with _session() as session:
        task = _setup(session)
        reservation = _reserve(session, task, due=FIRST_START)
        _block(session, task, at=FIRST_END - timedelta(minutes=2))
        rearmed = _reserve(session, task, due=FIRST_END - timedelta(minutes=1))

        assert rearmed.id == reservation.id
        assert rearmed.effective_claim_at == NEXT_START


@pytest.mark.parametrize("next_window_busy", [False, True])
def test_claim_rechecks_window_after_inflight_account_timeline(next_window_busy):
    with _session() as session:
        task = _setup(session)
        reservation = _reserve(session, task, due=FIRST_START)
        mine = _block(session, task, at=FIRST_START)
        mine.pacing_due_at = FIRST_START
        mine.pacing_slot_key = reservation.pacing_slot_key
        bind_account_pacing_reservation(reservation, mine)
        _block(session, task, at=FIRST_END - timedelta(minutes=2), status="executing")
        if next_window_busy:
            _block(session, task, at=NEXT_START, status="executing")
        now = FIRST_END - timedelta(minutes=1)

        decision = revalidate_action_pacing_before_claim(session, mine, now_value=now)

        expected = NEXT_START + PAIR_GAP if next_window_busy else NEXT_START
        assert not decision.allowed
        assert decision.effective_claim_at == expected
        assert mine.scheduled_at == expected
        assert reservation.effective_claim_at == expected
        assert mine.pacing_due_at == FIRST_START
        assert session.scalar(select(func.count()).select_from(Action)) == (3 if next_window_busy else 2)


def test_claim_rechecks_window_after_cross_account_group_gap(monkeypatch):
    monkeypatch.setattr(account_pacing_guard, "get_settings", lambda: SimpleNamespace(
        ai_group_send_pacing_min_gap_seconds=20,
    ))
    with _session() as session:
        task = _setup(session)
        reservation = _reserve(session, task, due=FIRST_START)
        mine = _block(session, task, at=FIRST_START)
        mine.pacing_due_at = FIRST_START
        mine.pacing_slot_key = reservation.pacing_slot_key
        bind_account_pacing_reservation(reservation, mine)
        other = _block(session, task, at=FIRST_END - timedelta(seconds=10), status="executing")
        other.account_id = 12
        decision = revalidate_action_pacing_before_claim(
            session, mine, now_value=FIRST_END - timedelta(seconds=1),
        )
        assert not decision.allowed
        assert decision.effective_claim_at == NEXT_START


def test_claim_after_source_deferral_preserves_release_and_reenters_legal_window():
    with _session() as session:
        task = _setup(session)
        reservation = _reserve(session, task, due=FIRST_START)
        mine = _block(session, task, at=FIRST_START)
        mine.pacing_due_at = FIRST_START
        mine.pacing_slot_key = reservation.pacing_slot_key
        bind_account_pacing_reservation(reservation, mine)
        source_release = FIRST_END + timedelta(minutes=2)
        mine.release_not_before_at = source_release
        mine.scheduled_at = source_release
        decision = revalidate_action_pacing_before_claim(session, mine, now_value=source_release)
        assert not decision.allowed
        assert decision.effective_claim_at == NEXT_START
        assert mine.release_not_before_at >= source_release
        assert mine.pacing_due_at == FIRST_START
        decision = revalidate_action_pacing_before_claim(session, mine, now_value=NEXT_START)
        assert decision.allowed
        assert reservation.effective_claim_at == NEXT_START


def test_claim_no_remaining_window_returns_original_deadline_failure():
    with _session() as session:
        task = _setup(session)
        reservation = _reserve(session, task, due=FIRST_START, deadline=FIRST_END)
        mine = _block(session, task, at=FIRST_START)
        mine.pacing_due_at = FIRST_START
        mine.pacing_slot_key = reservation.pacing_slot_key
        bind_account_pacing_reservation(reservation, mine)
        _block(session, task, at=FIRST_END - timedelta(minutes=2), status="executing")
        decision = revalidate_action_pacing_before_claim(
            session, mine, now_value=FIRST_END - timedelta(minutes=1),
        )
        assert not decision.allowed
        assert decision.reason_code == "pacing_claim_deadline_exceeded"
        assert mine.pacing_due_at == FIRST_START
