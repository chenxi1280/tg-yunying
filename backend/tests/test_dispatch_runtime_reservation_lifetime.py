from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.models import AccountBehaviorBudgetReservation, AccountPoolConcurrencyLease, RemoteInvocationFence
from app.services.task_center import dispatcher, runtime_resources, service
from test_engagement_runtime_resources import _session
from test_telegram_termination import _unknown

pytestmark = pytest.mark.no_postgres
WAIT_SECONDS = 2


class _Session:
    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def __init__(self, action=None):
        self.action = action

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def commit(self):
        return None

    def get(self, _model, _key):
        return self.action


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    monkeypatch.setattr(runtime_resources, "_ACTION_RESERVATIONS", {})
    monkeypatch.setattr(runtime_resources, "_IN_FLIGHT_ACCOUNTS", set())
    monkeypatch.setattr(runtime_resources, "_uses_fact_first_contract", lambda _action: True)
    monkeypatch.setattr(service, "record_worker_heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_dispatcher_concurrency", lambda: 2)


def _action(action_id="claimed-action"):
    return SimpleNamespace(
        id=action_id, status="executing", action_type="view_message", payload={},
    )


def _reserve(action):
    assert runtime_resources._reserve_runtime_resources(action)


def test_claim_error_releases_only_reservations_created_by_its_batch(monkeypatch):
    unrelated = _action("other-batch")
    current = _action()
    _reserve(unrelated)
    unrelated_reservation = runtime_resources._ACTION_RESERVATIONS[unrelated.id]

    def fail_claim(*_args, **_kwargs):
        _reserve(current)
        raise OperationalError("claim commit", {}, RuntimeError("database down"))

    monkeypatch.setattr(service, "claim_actions", fail_claim)
    with pytest.raises(OperationalError, match="database down"):
        service.drain_task_dispatcher(_Session, 1)

    assert runtime_resources._ACTION_RESERVATIONS == {
        unrelated.id: unrelated_reservation,
    }


@pytest.mark.parametrize("current_status", [None, "failed", "closed_unknown"])
def test_action_missing_or_no_longer_executing_releases_batch_bookkeeping(
    monkeypatch, current_status,
):
    claimed = _action()
    current = _action() if current_status else None
    if current is not None:
        current.status = current_status
    monkeypatch.setattr(service, "claim_actions", lambda *_args, **_kwargs: _claim(claimed))

    assert service.drain_task_dispatcher(lambda: _Session(current), 1) == 0

    assert runtime_resources._ACTION_RESERVATIONS == {}
    if current is not None:
        assert current.status == current_status


def _claim(*actions):
    for action in actions:
        _reserve(action)
    return list(actions)


def test_finalization_error_releases_local_bookkeeping_without_hiding_error(monkeypatch):
    action = _action()
    monkeypatch.setattr(service, "claim_actions", lambda *_args, **_kwargs: _claim(action))
    monkeypatch.setattr(dispatcher, "_fulfillment_route_allows_gateway", lambda *_args: True)
    monkeypatch.setattr(dispatcher, "_legacy_content_scope_takeover_pending", lambda _action: False)
    monkeypatch.setattr(dispatcher, "_dispatch_action", lambda *_args, **_kwargs: False)

    def fail_finalization(*_args, **_kwargs):
        raise RuntimeError("durable result commit failed")

    monkeypatch.setattr(dispatcher, "_finalize_dispatch_action", fail_finalization)
    with pytest.raises(RuntimeError, match="durable result commit failed"):
        service.drain_task_dispatcher(lambda: _Session(action), 1)

    assert runtime_resources._ACTION_RESERVATIONS == {}
    assert action.status == "executing"


def test_failed_future_does_not_release_other_running_future(monkeypatch):
    actions = [_action("failed-future"), _action("running-future")]
    running = threading.Event()
    failed = threading.Event()
    finish = threading.Event()
    errors: list[BaseException] = []
    monkeypatch.setattr(service, "claim_actions", lambda *_args, **_kwargs: _claim(*actions))

    def dispatch(_sessions, action_id):
        if action_id == "running-future":
            running.set()
            assert finish.wait(WAIT_SECONDS)
            return 1
        assert running.wait(WAIT_SECONDS)
        failed.set()
        raise RuntimeError("one future failed")

    def drain():
        try:
            service.drain_task_dispatcher(_Session, 2)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(service, "_dispatch_claimed_action", dispatch)
    thread = threading.Thread(target=drain)
    thread.start()
    try:
        assert failed.wait(WAIT_SECONDS)
        assert thread.is_alive()
        assert "running-future" in runtime_resources._ACTION_RESERVATIONS
    finally:
        finish.set()
        thread.join(WAIT_SECONDS)

    assert not thread.is_alive()
    assert len(errors) == 1 and str(errors[0]) == "one future failed"
    assert runtime_resources._ACTION_RESERVATIONS == {}


def test_batch_release_preserves_same_action_successor_created_on_another_thread():
    action = _action()
    with runtime_resources.dispatch_runtime_reservation_scope():
        _reserve(action)
        predecessor = runtime_resources._ACTION_RESERVATIONS[action.id]
        thread = threading.Thread(target=_reserve, args=(action,))
        thread.start()
        thread.join(WAIT_SECONDS)
        assert not thread.is_alive()
        successor = runtime_resources._ACTION_RESERVATIONS[action.id]
        assert successor is not predecessor

    assert runtime_resources._ACTION_RESERVATIONS[action.id] is successor


def test_legacy_reservation_error_releases_intermediate_account_registration(monkeypatch):
    action = _action()
    action.account_id = 91
    monkeypatch.setattr(runtime_resources, "_uses_fact_first_contract", lambda _action: False)

    def fail_token_reservation(_action):
        raise RuntimeError("token backend unavailable")

    monkeypatch.setattr(runtime_resources, "_reserve_redis_token", fail_token_reservation)
    with pytest.raises(RuntimeError, match="token backend unavailable"):
        with runtime_resources.dispatch_runtime_reservation_scope():
            _reserve(action)

    assert runtime_resources._ACTION_RESERVATIONS == {}
    assert runtime_resources._IN_FLIGHT_ACCOUNTS == set()


def test_local_batch_cleanup_preserves_durable_unknown_and_transport_occupancy(monkeypatch):
    with _session() as session:
        action, attempt = _unknown(session)
        factory = sessionmaker(session.get_bind())
        monkeypatch.setattr(service, "claim_actions", lambda *_args, **_kwargs: _claim(action))
        original_result = dict(attempt.result_snapshot)

        assert service.drain_task_dispatcher(factory, 1) == 0

        assert runtime_resources._ACTION_RESERVATIONS == {}
        session.expire_all()
        assert action.status == "unknown_after_send"
        assert attempt.status == "result_unknown"
        assert attempt.result_snapshot == original_result
        assert session.scalar(select(AccountPoolConcurrencyLease)).state == "remote_unknown"
        assert session.scalar(select(AccountBehaviorBudgetReservation)).state == "unknown"
        fence = session.scalar(select(RemoteInvocationFence))
        assert fence.state == "remote_unknown"
        assert fence.transport_termination_state == "cancellation_unconfirmed"
        assert fence.transport_terminated_at is None
