from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import ViewRemoteFact
from app.services.task_center.executors.channel_view import build_plan as build_view
from tests.channel_view_coverage_support import (
    add_lifetime_fact,
    add_message,
    add_view_task,
    confirm_actions,
    new_session,
    seed_channel_scenario,
    view_actions,
)


pytestmark = pytest.mark.no_postgres


def test_channel_view_spreads_daily_coverage_across_messages(monkeypatch) -> None:
    now = datetime(2026, 8, 28, 10, 0)
    _set_view_clock(monkeypatch, now)
    with new_session() as session:
        scenario = seed_channel_scenario(session, channel_id=103, account_count=10)
        messages = [
            add_message(
                session,
                channel=scenario.channel,
                message_id=message_id,
                published_at=now - timedelta(minutes=age),
            )
            for message_id, age in [(61, 10), (62, 5)]
        ]
        task = add_view_task(
            session,
            channel=scenario.channel,
            messages=messages,
            task_id="task-cross-message-coverage",
            daily_target=5,
            total_target=50,
        )

        assert build_view(session, task) == 10
        actions = view_actions(session, task)
        assert {action.account_id for action in actions} == set(range(1, 11))
        confirm_actions(scenario, actions=actions, confirmed_at=now)
        assert session.query(ViewRemoteFact).count() == 10


def test_channel_view_next_day_uses_new_message_lifetime_capacity(monkeypatch) -> None:
    day_one = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
    with new_session() as session:
        scenario = seed_channel_scenario(session, channel_id=104, account_count=5)
        first = add_message(
            session,
            channel=scenario.channel,
            message_id=71,
            published_at=day_one - timedelta(hours=1),
        )
        task = add_view_task(
            session,
            channel=scenario.channel,
            messages=[first],
            task_id="task-next-day-coverage",
            daily_target=5,
            total_target=5,
        )
        _set_view_clock(monkeypatch, day_one)
        assert build_view(session, task) == 5
        confirm_actions(scenario, actions=view_actions(session, task), confirmed_at=day_one)

        day_two = day_one + timedelta(days=1)
        _set_view_clock(monkeypatch, day_two)
        assert build_view(session, task) == 0
        second = add_message(
            session,
            channel=scenario.channel,
            message_id=72,
            published_at=day_two - timedelta(minutes=10),
        )
        task.type_config = {**task.type_config, "message_ids": [first.id, second.id]}
        session.commit()
        assert build_view(session, task) == 5
        pending = [action for action in view_actions(session, task) if action.status != "success"]
        assert {action.account_id for action in pending} == set(range(1, 6))


def _set_view_clock(monkeypatch, value: datetime) -> None:
    monkeypatch.setattr("app.services.task_center.executors.channel_view._now", lambda: value)
    monkeypatch.setattr("app.services.task_center.daily_ledgers._now", lambda: value)


def test_channel_view_uses_maximum_matching_for_uneven_identity_sets(monkeypatch) -> None:
    now = datetime(2026, 8, 28, 10, 0)
    _set_view_clock(monkeypatch, now)
    with new_session() as session:
        scenario = seed_channel_scenario(session, channel_id=105, account_count=2)
        newest = add_message(
            session,
            channel=scenario.channel,
            message_id=81,
            published_at=now - timedelta(minutes=5),
        )
        oldest = add_message(
            session,
            channel=scenario.channel,
            message_id=82,
            published_at=now - timedelta(hours=2),
        )
        add_lifetime_fact(
            scenario,
            message=oldest,
            account=scenario.accounts[1],
            confirmed_at=now - timedelta(days=1),
        )
        task = add_view_task(
            session,
            channel=scenario.channel,
            messages=[newest, oldest],
            task_id="task-bipartite-matching",
            daily_target=1,
            total_target=10,
        )

        assert build_view(session, task) == 2
        assignments = {
            action.payload["channel_message_id"]: action.account_id
            for action in view_actions(session, task)
        }
        assert assignments == {newest.id: 2, oldest.id: 1}
