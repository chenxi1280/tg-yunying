from datetime import timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import ChannelSourceDecision, ListenerSourceState, TaskSourceSubscription
from app.services.task_center import channel_source_intake as intake
from app.services.task_center.channel_source_observation import source_deadline_outcome
from app.timezone import BEIJING_TZ
from engine_source_test_support import NOW, message, seed_source_session


pytestmark = pytest.mark.no_postgres


def _observed_day(session, task, ledger, *, gap=False):
    start = NOW.replace(hour=0)
    end = start+timedelta(days=1)
    ledger.period_start_at = start.replace(tzinfo=BEIJING_TZ).astimezone(timezone.utc)
    ledger.planning_anchor_at = NOW.replace(tzinfo=BEIJING_TZ).astimezone(timezone.utc)
    ledger.deadline_at = end.replace(tzinfo=BEIJING_TZ).astimezone(timezone.utc)
    state = ListenerSourceState(id="source", tenant_id=1, source_type="channel", source_peer_id="1",
        last_event_at=start, observed_at=end, backfill_until=NOW if gap else None)
    session.add(state)
    session.flush()
    session.add(TaskSourceSubscription(tenant_id=1, task_id=task.id, lifecycle_epoch=1,
        source_type="channel", source_peer_hash="hash", listener_source_state_id=state.id))
    session.commit()
    session.expire_all()


@pytest.mark.parametrize("mode,expected", [
    ("continuous_event_driven", "neutral_no_opportunity"),
    ("finite_existing_sources", "missed_no_source"),
    ("promised_daily_sources", "missed_promised_source"),
])
@pytest.mark.parametrize("gap", [False, True])
def test_real_day_source_modes_distinguish_absence_from_listener_gap(mode, expected, gap):
    session, task, ledger, _ = seed_source_session()
    with session:
        task.type_config = {**task.type_config, "source_expectation_mode": mode}
        _observed_day(session, task, ledger, gap=gap)
        assert source_deadline_outcome(session, task, ledger) == ("source_ingestion_unproven" if gap else expected)


def test_expired_old_accepted_source_does_not_hide_today_without_opportunity(monkeypatch):
    monkeypatch.setattr(intake, "_now", lambda: NOW)
    session, task, ledger, _ = seed_source_session()
    with session:
        old = message(session, 1, at=NOW-timedelta(days=4))
        old.created_at = NOW-timedelta(days=4)
        intake.unified_source_intake(session, task, [old], config=task.type_config, observation_complete=True)
        _observed_day(session, task, ledger)
        assert source_deadline_outcome(session, task, ledger) == "neutral_no_opportunity"


def test_late_intake_does_not_blame_publisher_for_a_post_published_that_day(monkeypatch):
    monkeypatch.setattr(intake, "_now", lambda: NOW)
    session, task, ledger, _ = seed_source_session()
    with session:
        task.type_config = {**task.type_config, "source_expectation_mode": "promised_daily_sources"}
        row = message(session, 1, at=NOW-timedelta(hours=1))
        intake.unified_source_intake(session, task, [row], config=task.type_config, observation_complete=True)
        decision = session.scalar(select(ChannelSourceDecision))
        decision.observed_at = NOW+timedelta(days=1)
        _observed_day(session, task, ledger)
        assert source_deadline_outcome(session, task, ledger) is None


def test_late_comment_collection_does_not_extend_published_source_deadline():
    from app.services.task_center.channel_source_policy import source_window_end
    from app.services.task_center.source_pacing import rolling_source_window
    session, task, _, _ = seed_source_session(task_type="channel_comment")
    with session:
        row = message(session, 1, at=NOW-timedelta(days=4))
        row.created_at = NOW
        assert source_window_end(task, row) == rolling_source_window(task, row.published_at)[1]
        assert source_window_end(task, row) < NOW
