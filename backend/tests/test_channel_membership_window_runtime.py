from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, ExecutionAttempt, OperationTarget, Task, Tenant, TgAccount
from app.schemas.task_center import ChannelLikeConfig, ChannelViewConfig, ChannelCommentConfig, TaskSettingsUpdate
from app.services.task_center.channel_membership_schedule import channel_membership_schedule
from app.services.task_center.channel_membership_runtime import membership_runtime_wait


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 7, 10)
MODELS = (ChannelLikeConfig, ChannelViewConfig, ChannelCommentConfig)


@pytest.mark.parametrize("count", [0, 1, 2, 10, 100, 481, 2402, 5760, 5761])
@pytest.mark.parametrize("endpoint", ["low", "high"])
def test_membership_schedule_preserves_humanized_window_and_gap(monkeypatch, count, endpoint):
    monkeypatch.setattr("app.services.task_center.channel_membership_schedule.random.randint",
                        lambda low, high: low if endpoint == "low" else high)
    times = channel_membership_schedule(count, NOW)
    assert len(times) == count
    if not times:
        return
    assert times[0] == NOW
    if count > 1:
        assert timedelta(hours=10) <= times[-1] - times[0] <= timedelta(hours=24)
    assert all(right - left >= timedelta(seconds=15) for left, right in zip(times, times[1:]))


@pytest.mark.parametrize("count", [2, 100, 240, 479, 480, 481, 2402, 5760, 5761])
def test_opposite_jitter_extremes_do_not_reduce_minimum_gap(monkeypatch, count):
    calls = iter(range(count))
    monkeypatch.setattr("app.services.task_center.channel_membership_schedule.random.randint",
                        lambda low, high: high if next(calls) % 2 == 0 else low)
    times = channel_membership_schedule(count, NOW)
    assert all(right - left >= timedelta(seconds=15) for left, right in zip(times, times[1:]))


def test_membership_window_capacity_error_is_explicit():
    with pytest.raises(ValueError, match="membership_schedule_capacity_exceeded"):
        channel_membership_schedule(5762, NOW)


def test_membership_intervals_are_not_a_fixed_cadence(monkeypatch):
    calls = iter(range(10))
    monkeypatch.setattr("app.services.task_center.channel_membership_schedule.random.randint",
                        lambda low, high: high if next(calls) % 2 == 0 else low)
    times = channel_membership_schedule(10, NOW)
    assert len({right - left for left, right in zip(times, times[1:])}) > 1


@pytest.mark.parametrize("hours", [1, 2, 6])
def test_retired_window_is_explicitly_deprecated_and_not_serialized(hours):
    for model in (*MODELS, TaskSettingsUpdate):
        target = {} if model is TaskSettingsUpdate else {"target_channel_id": 1}
        config = model(membership_schedule_window_hours=hours, **target)
        assert model.model_fields["membership_schedule_window_hours"].deprecated
        assert "membership_schedule_window_hours" not in config.model_dump(exclude_unset=True)


@pytest.mark.parametrize("hours", [0, 7, -1, 1.5, "2", True])
def test_invalid_retired_window_rejected_by_create_and_update(hours):
    for model in (*MODELS, TaskSettingsUpdate):
        target = {} if model is TaskSettingsUpdate else {"target_channel_id": 1}
        with pytest.raises(ValidationError):
            model(membership_schedule_window_hours=hours, **target)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        seed_membership_runtime(current)
        yield current
    engine.dispose()


def seed_membership_runtime(session):
    session.add(Tenant(id=1, name="membership"))
    session.flush()
    for number in range(1, 5):
        session.add(TgAccount(id=number, tenant_id=1, display_name=str(number), phone_masked="***"))
        session.add(Task(id=f"task-{number}", tenant_id=1, name=str(number), type="channel_like", status="running"))
    session.add(OperationTarget(id=1, tenant_id=1, target_type="channel", tg_peer_id="-1001", title="shared"))
    session.add(OperationTarget(id=4, tenant_id=1, target_type="channel", tg_peer_id="-1002", title="other"))
    session.commit()


def membership_action(number, *, target_id=None):
    return Action(id=f"action-{number}", tenant_id=1, task_id=f"task-{number}",
                  task_type="channel_like", action_type="ensure_target_membership", account_id=number,
                  status="executing", payload={"channel_target_id": target_id or (1 if number < 4 else 4)})


def add_running_attempt(session, action, *, completed=None):
    attempt = ExecutionAttempt(tenant_id=1, action_id=action.id, account_id=action.account_id,
                               attempt_no=1, before_call_at=NOW, gateway_call_started_at=NOW,
                               after_call_at=completed, status="success" if completed else "gateway_call_started")
    session.add(attempt)
    session.flush()
    return attempt


def test_same_peer_across_tasks_is_limited_but_other_peer_is_independent(session):
    for number in (1, 2):
        action = membership_action(number)
        session.add(action)
        session.flush()
        add_running_attempt(session, action)
    third = membership_action(3)
    wait = membership_runtime_wait(session, third, target=session.get(OperationTarget, 1), now=NOW)
    assert wait.code == "channel_membership_concurrency_wait"
    fourth = membership_action(4)
    assert membership_runtime_wait(session, fourth, target=session.get(OperationTarget, 4), now=NOW) is None


@pytest.mark.parametrize("after_call", [None, NOW])
def test_unknown_unfinished_attempt_keeps_channel_capacity(session, after_call):
    for number in (1, 2):
        action = membership_action(number)
        action.status = "unknown_after_send"
        action.lease_expires_at = NOW - timedelta(hours=1)
        session.add(action)
        session.flush()
        attempt = add_running_attempt(session, action)
        attempt.status = "result_unknown"
        attempt.after_call_at = after_call
    session.flush()
    wait = membership_runtime_wait(session, membership_action(3), target=session.get(OperationTarget, 1), now=NOW)
    assert wait.code == "channel_membership_concurrency_wait"


def test_success_releases_peer_slot_but_account_has_sixty_second_cooldown(session):
    previous = membership_action(1)
    session.add(previous)
    session.flush()
    add_running_attempt(session, previous, completed=NOW)
    same_account = membership_action(4)
    same_account.account_id = 1
    target = session.get(OperationTarget, 4)
    wait = membership_runtime_wait(session, same_account, target=target, now=NOW + timedelta(seconds=59))
    assert wait.code == "account_membership_cooldown"
    assert wait.retry_at == NOW + timedelta(seconds=60)
    assert membership_runtime_wait(session, same_account, target=target, now=NOW + timedelta(seconds=60)) is None


def test_dispatcher_does_not_create_attempt_when_channel_is_full(session, monkeypatch):
    from app.services.task_center import dispatcher
    from app.services.task_center.payloads import EnsureChannelMembershipPayload
    from app.services import outbound_target_gate

    for number in (1, 2):
        action = membership_action(number)
        session.add(action)
        session.flush()
        add_running_attempt(session, action)
    third = membership_action(3)
    session.add(third)
    session.flush()
    target = session.get(OperationTarget, 1)
    monkeypatch.setattr(dispatcher, "_attempt_target", lambda *_: target)
    monkeypatch.setattr(outbound_target_gate, "evaluate_outbound_target_gate", lambda *a, **kw: None)
    monkeypatch.setattr(dispatcher, "_release_runtime_resources", lambda _: None)
    result = dispatcher._reserve_channel_membership_attempt(
        session, third, session.get(TgAccount, 3),
        EnsureChannelMembershipPayload(channel_target_id=1, channel_id="-1001"),
    )
    assert result is None
    assert third.status == "pending"
    assert third.result["error_code"] == "channel_membership_concurrency_wait"
    assert session.query(ExecutionAttempt).filter_by(action_id=third.id).count() == 0
