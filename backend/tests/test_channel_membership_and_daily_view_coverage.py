from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import Action, ViewRemoteFact
from app.services.task_center.channel_membership import (
    channel_member_accounts,
    gate_channel_membership,
)
from app.services.task_center.executors.channel_like import build_plan as build_like
from app.services.task_center.executors.channel_view import build_plan as build_view
from tests.channel_view_coverage_support import (
    add_lifetime_fact,
    add_like_task,
    add_message,
    add_view_task,
    confirm_actions,
    link_accounts,
    new_session,
    seed_channel_scenario,
    view_actions,
)


pytestmark = pytest.mark.no_postgres


def test_channel_tasks_require_per_account_membership() -> None:
    with new_session() as session:
        scenario = seed_channel_scenario(
            session,
            channel_id=101,
            account_count=10,
            linked=False,
        )
        task = add_view_task(
            session,
            channel=scenario.channel,
            messages=[],
            task_id="task-view-membership",
            daily_target=10,
            total_target=10,
        )

        gate = gate_channel_membership(session, task, scenario.channel)
        assert gate.ready is False
        assert gate.created == 10

        link_accounts(session, channel=scenario.channel, accounts=scenario.accounts)
        assert gate_channel_membership(session, task, scenario.channel).ready is True
        members = channel_member_accounts(
            session,
            task,
            scenario.channel,
            scenario.accounts,
        )
        assert len(members) == 10


def test_channel_like_waits_for_membership_before_reactions() -> None:
    with new_session() as session:
        scenario = seed_channel_scenario(
            session,
            channel_id=102,
            account_count=5,
            linked=False,
        )
        message = add_message(
            session,
            channel=scenario.channel,
            message_id=51,
            published_at=datetime(2026, 8, 28, 10, 0),
        )
        task = add_like_task(
            session,
            channel=scenario.channel,
            message=message,
            target=5,
        )

        assert build_like(session, task) == 5
        assert _action_count(session, "like_message") == 0
        link_accounts(session, channel=scenario.channel, accounts=scenario.accounts)
        assert build_like(session, task) == 5
        assert _action_count(session, "like_message") == 5


def _action_count(session, action_type: str) -> int:
    statement = select(Action).where(Action.action_type == action_type)
    return len(list(session.scalars(statement)))


def test_channel_view_spreads_daily_coverage_across_messages() -> None:
    now = datetime(2026, 8, 28, 10, 0)
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


def test_channel_view_uses_maximum_matching_for_uneven_identity_sets() -> None:
    now = datetime(2026, 8, 28, 10, 0)
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
