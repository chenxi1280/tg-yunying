from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Action, ExecutionAttempt, FulfillmentRemoteFact
from app.services.task_center.executors.channel_view import build_plan
from app.timezone import BEIJING_TZ
from scripts.abandon_channel_historical_backlog import abandon_channel_historical_backlog
from tests.channel_view_coverage_support import new_session
from tests.test_channel_view_daily_identity_lifecycle import _seed_two_tasks, _set_view_clock

pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 8, 29, 10, 0, tzinfo=BEIJING_TZ)
CUTOFF = NOW + timedelta(hours=1)


def _seed(session, monkeypatch):
    _set_view_clock(monkeypatch, NOW)
    task, _ = _seed_two_tasks(session, channel_id=86, current=NOW)
    assert build_plan(session, task) == 1
    action = session.scalar(select(Action).where(Action.task_id == task.id))
    session.commit()
    return action


def test_preview_apply_and_repeat_use_real_settlement(monkeypatch):
    with new_session() as session:
        action = _seed(session, monkeypatch)
        before = action.status
        result = abandon_channel_historical_backlog(session, cutoff=CUTOFF, apply=False)
        assert result['candidate_count'] == 1
        assert action.status == before
        assert session.scalar(select(FulfillmentRemoteFact)) is None
        result = abandon_channel_historical_backlog(session, cutoff=CUTOFF, apply=True)
        assert result['settled_count'] == 1
        assert action.status == 'skipped'
        assert session.scalar(select(FulfillmentRemoteFact)).fact_kind == 'safely_not_executed'
        assert abandon_channel_historical_backlog(
            session, cutoff=CUTOFF, apply=True,
        )['settled_count'] == 0


@pytest.mark.parametrize('excluded', ['cutoff', 'gateway', 'task', 'status'])
def test_excludes_ineligible_actions(monkeypatch, excluded):
    with new_session() as session:
        action = _seed(session, monkeypatch)
        task_ids = None
        if excluded == 'cutoff':
            action.scheduled_at = CUTOFF
        if excluded == 'gateway':
            session.add(ExecutionAttempt(
                action_id=action.id, tenant_id=action.tenant_id,
                gateway_call_started_at=NOW,
            ))
        if excluded == 'task':
            task_ids = {'another-task'}
        if excluded == 'status':
            action.status = 'executing'
        session.commit()
        result = abandon_channel_historical_backlog(
            session, cutoff=CUTOFF, apply=True, task_ids=task_ids,
        )
        assert result['candidate_count'] == result['settled_count'] == 0


def test_rechecks_rescheduled_candidate_before_settlement(monkeypatch):
    with new_session() as session:
        action = _seed(session, monkeypatch)
        original = session.scalars
        def scalars(stmt, *args, **kwargs):
            result = original(stmt, *args, **kwargs)
            if stmt.column_descriptions[0]['expr'] is Action.id:
                ids = list(result)
                action.scheduled_at = CUTOFF + timedelta(hours=1)
                session.commit()
                return ids
            return result
        monkeypatch.setattr(session, 'scalars', scalars)
        result = abandon_channel_historical_backlog(session, cutoff=CUTOFF, apply=True)
        assert result['candidate_count'] == 1
        assert result['settled_count'] == 0
        assert action.status == 'pending'
