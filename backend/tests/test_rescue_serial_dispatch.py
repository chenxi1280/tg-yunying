from threading import Event

import pytest

from app.config import Settings
from app.models import Action
from app.services._common import _now
from app.services.task_center import dispatcher as claims
from app.services.task_center import runtime_resources as resources
from app.services.task_center.continuous_dispatcher import ContinuousDispatcher, DispatcherCallbacks

pytestmark = pytest.mark.no_postgres
WAIT_SECONDS = 3
RESCUE_COUNT = 5


@pytest.fixture(autouse=True)
def local_resources(monkeypatch):
    settings = Settings(enable_redis_token_bucket=False, enable_redis_account_inflight=False)
    monkeypatch.setattr(resources, "get_settings", lambda: settings)


def _action(identifier, account_id, action_type="invite_group_account"):
    return Action(id=identifier, account_id=account_id, action_type=action_type,
                  scheduled_at=_now(), status="pending", result={})


def test_busy_admin_is_skipped_without_delaying_other_rescue_candidates():
    current = _action("serial-busy", 99901)
    waiting = [_action(f"serial-wait-{index}", 99901) for index in range(RESCUE_COUNT)]
    other = _action("serial-other", 99902)
    ordinary = _action("serial-send", 99903, "send_message")
    snapshots = [(row.status, row.scheduled_at, dict(row.result)) for row in waiting]
    with resources.dispatch_runtime_reservation_scope():
        assert resources._reserve_runtime_resources(current)
        assert claims._claimable_candidates([*waiting, other, ordinary]) == [other, ordinary]
        assert snapshots == [(row.status, row.scheduled_at, row.result) for row in waiting]
    assert claims._claimable_candidates(waiting) == waiting[:1]


def test_five_rescues_complete_serially_while_unrelated_slow_action_is_running():
    slow_started, release_slow = Event(), Event()
    slow = _action("serial-slow", 99801, "ensure_target_membership")
    rescues = [_action(f"serial-rescue-{index}", 99802) for index in range(RESCUE_COUNT)]
    rows = {row.id: row for row in [slow, *rescues]}
    completed = []

    def claim(limit):
        candidates = claims._claimable_candidates([row for row in rows.values() if row.status == "pending"])
        selected = candidates[:limit]
        for row in selected:
            assert resources._reserve_runtime_resources(row), row.result
            row.status = "executing"
        return [row.id for row in selected]

    def dispatch(identifier):
        if identifier == slow.id:
            slow_started.set()
            assert release_slow.wait(WAIT_SECONDS)
        else:
            assert rows[identifier].account_id in resources._IN_FLIGHT_ACCOUNTS
            completed.append(identifier)
        rows[identifier].status = "success"
        return 1

    executor = ContinuousDispatcher()
    callbacks = DispatcherCallbacks(claim, dispatch)
    try:
        for _ in rescues:
            executor.drain(callbacks, capacity=2, limit=2)
            assert executor.completed.wait(WAIT_SECONDS)
        assert slow_started.is_set() and not release_slow.is_set()
        assert completed == [row.id for row in rescues]
        assert all("runtime_resource_reason" not in row.result for row in rescues)
    finally:
        release_slow.set()
        executor.close()
    assert all(identifier not in resources._ACTION_RESERVATIONS for identifier in rows)
