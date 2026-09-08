from collections import deque
from threading import Event

import pytest

from app.services.task_center.continuous_dispatcher import ContinuousDispatcher, DispatcherCallbacks
from app.services.task_center import runtime_resources as resources

pytestmark = pytest.mark.no_postgres
WAIT_SECONDS = 2


def test_fast_completion_replenishes_while_slow_admission_is_still_running():
    slow_started, release_slow, fast_done, next_started = (Event() for _ in range(4))
    queue = deque(("membership", "send-1", "send-2"))
    requested = []

    def claim(limit):
        requested.append(limit)
        return [queue.popleft() for _ in range(min(limit, len(queue)))]

    def dispatch(action_id):
        if action_id == "membership":
            slow_started.set()
            assert release_slow.wait(WAIT_SECONDS)
        elif action_id == "send-1":
            fast_done.set()
        else:
            next_started.set()
        return 1

    dispatcher = ContinuousDispatcher()
    callbacks = DispatcherCallbacks(claim, dispatch)
    try:
        dispatcher.drain(callbacks, capacity=2, limit=2)
        assert slow_started.wait(WAIT_SECONDS) and fast_done.wait(WAIT_SECONDS)
        assert dispatcher.completed.wait(WAIT_SECONDS)
        dispatcher.drain(callbacks, capacity=2, limit=2)
        assert next_started.wait(WAIT_SECONDS)
        assert not release_slow.is_set()
        assert requested == [2, 1]
    finally:
        release_slow.set()
        dispatcher.close()


def test_claim_scope_does_not_release_reservation_owned_by_running_action():
    release, started = Event(), Event()
    original = resources._RuntimeReservation(account_id=None)

    def claim(limit):
        with resources._IN_FLIGHT_LOCK:
            resources._record_runtime_reservation("owned", original)
        return ["owned"]

    def dispatch(action_id):
        started.set()
        assert release.wait(WAIT_SECONDS)
        return 1

    dispatcher = ContinuousDispatcher()
    try:
        dispatcher.drain(DispatcherCallbacks(claim, dispatch), capacity=1, limit=1)
        assert started.wait(WAIT_SECONDS)
        assert resources._ACTION_RESERVATIONS["owned"] is original
    finally:
        release.set()
        dispatcher.close()
    assert "owned" not in resources._ACTION_RESERVATIONS


def test_completed_owner_cannot_release_successor_reservation():
    old = resources._RuntimeReservation(account_id=None)
    new = resources._RuntimeReservation(account_id=None)
    with resources.dispatch_runtime_reservation_scope():
        with resources._IN_FLIGHT_LOCK:
            resources._record_runtime_reservation("reused", old)
        captured = resources.transfer_dispatch_reservations(("reused",))
    with resources.dispatch_runtime_reservation_scope():
        with resources._IN_FLIGHT_LOCK:
            resources._record_runtime_reservation("reused", new)
        resources.release_transferred_dispatch_reservations(captured)
        assert resources._ACTION_RESERVATIONS["reused"] is new
    assert "reused" not in resources._ACTION_RESERVATIONS


def test_close_waits_for_execution_and_does_not_claim_more():
    completed = Event()
    claims = []
    dispatcher = ContinuousDispatcher()

    def claim(limit):
        claims.append(limit)
        return ["send"]

    def dispatch(action_id):
        completed.set()
        return 1

    dispatcher.drain(DispatcherCallbacks(claim, dispatch), capacity=1, limit=1)
    dispatcher.close()
    assert completed.is_set() and claims == [1]


def test_production_drain_entry_submits_without_waiting_for_slow_action(monkeypatch):
    from app.services.task_center import service
    from app.services.task_center.continuous_dispatcher import continuous_dispatcher_scope

    started, release = Event(), Event()
    monkeypatch.setattr(service, "_lane_concurrency", lambda _lane: 1)
    monkeypatch.setattr(service, "_claim_dispatcher_ids", lambda *args, **kwargs: ("slow",))

    def dispatch(_factory, action_id):
        started.set()
        assert release.wait(WAIT_SECONDS)
        return 1

    monkeypatch.setattr(service, "_dispatch_claimed_action", dispatch)
    with continuous_dispatcher_scope(enabled=True):
        try:
            assert service.drain_task_dispatcher(None, limit=1) == 0
            assert started.wait(WAIT_SECONDS)
        finally:
            release.set()
