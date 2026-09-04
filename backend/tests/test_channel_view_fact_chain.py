from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.integrations.telegram.contracts import OperationResult
from app.models import (
    ChannelViewDailyIdentityOwner,
    ExecutionAttempt,
    TgAccount,
    ViewRemoteFact,
)
from app.services.task_center.account_coverage import task_account_coverage
from app.services.task_center.dispatcher import _dispatch_view, _finalize_dispatch_action
from app.services.task_center.executors.channel_view import build_plan as build_view
from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION
from app.services.task_center.fulfillment_remote_facts import ensure_action_obligation
from app.services.task_center.payloads import ViewMessagePayload
from app.timezone import BEIJING_TZ
from tests.channel_view_coverage_support import (
    add_message,
    add_view_task,
    new_session,
    seed_channel_scenario,
    view_actions,
)


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("linked", [True, False])
def test_channel_view_fact_first_execution_chain(monkeypatch, linked) -> None:
    now = datetime(2026, 8, 28, 0, 0, 10, tzinfo=BEIJING_TZ)
    with new_session() as session:
        scenario = seed_channel_scenario(session, channel_id=106, account_count=3, linked=linked)
        scenario.channel.can_send = linked
        message = add_message(
            session,
            channel=scenario.channel,
            message_id=91,
            published_at=now - timedelta(minutes=10),
        )
        task = add_view_task(
            session,
            channel=scenario.channel,
            messages=[message],
            task_id="task-production-chain",
            daily_target=3,
            total_target=30,
            created_at=now - timedelta(minutes=10),
        )
        task.fulfillment_contract_version = CURRENT_CONTRACT_VERSION
        task.pacing_config = _pacing_config()
        session.commit()

        _set_clock(monkeypatch, now)
        assert build_view(session, task) == 3
        actions = view_actions(session, task)
        _install_gateway_success(monkeypatch)
        _dispatch_actions(session, actions=actions, monkeypatch=monkeypatch)

        action_ids = [action.id for action in actions]
        attempts = session.scalars(
            select(ExecutionAttempt).where(ExecutionAttempt.action_id.in_(action_ids))
        ).all()
        assert len(attempts) == 3
        attempt_evidence = [
            (attempt.status, attempt.failure_type, attempt.failure_detail)
            for attempt in attempts
        ]
        assert all(attempt.status == "success" for attempt in attempts), attempt_evidence
        assert session.query(ViewRemoteFact).count() == 3
        owners = session.scalars(select(ChannelViewDailyIdentityOwner)).all()
        assert len(owners) == 3
        assert {owner.state for owner in owners} == {"confirmed"}
        assert task_account_coverage(session, task)["coverage_percent"] == 100


def _pacing_config() -> dict:
    return {
        "mode": "fixed",
        "interval_seconds_min": 0,
        "interval_seconds_max": 0,
        "jitter_percent": 0,
        "operation_profile": {
            "hourly_activity_curve": [
                1 if hour in {0, 8, 16} else 0 for hour in range(24)
            ]
        },
    }


def _set_clock(monkeypatch, value: datetime) -> None:
    targets = [
        "app.services._common._now",
        "app.services.task_center.dispatcher._now",
        "app.services.task_center.source_pacing_admission._now",
        "app.services.task_center.channel_fulfillment._now",
        "app.services.task_center.daily_ledgers._now",
        "app.services.task_center.account_pool._now",
        "app.services.task_center.account_coverage._now",
        "app.services.task_center.executors.channel_view_pacing._now",
        "app.services.task_center.executors.channel_view._now",
    ]
    for target in targets:
        monkeypatch.setattr(target, lambda current=value: current)
    monkeypatch.setattr(
        "app.services.task_center.pacing_stratified._deterministic_offset_ratio",
        lambda *_args, **_kwargs: 0.05,
    )


def _install_gateway_success(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.view_channel_message",
        lambda *_args, **_kwargs: OperationResult(ok=True),
    )


def _dispatch_actions(session, *, actions, monkeypatch) -> None:
    from app.models.planner_runtime import SourcePacingState
    from app.services.task_center.source_pacing_admission import _source_admission_spec

    credentials = MagicMock()
    for action in actions:
        state = session.scalar(
            select(SourcePacingState).where(SourcePacingState.tenant_id == 1)
        )
        assert ensure_action_obligation(session, action) is True
        spec = _source_admission_spec(session, action)
        due_at = spec.release_at
        if state and state.next_call_not_before_at:
            due_at = max(due_at, state.next_call_not_before_at)
        _set_clock(monkeypatch, due_at)
        account = session.get(TgAccount, action.account_id)
        payload = ViewMessagePayload(**(action.payload or {}))
        assert _dispatch_view(
            session, action, account=account,
            credentials=credentials, payload=payload,
        ) is True
        _finalize_dispatch_action(session, action)
        session.flush()
    session.commit()
