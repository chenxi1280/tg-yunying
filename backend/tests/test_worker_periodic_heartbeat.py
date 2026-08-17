from __future__ import annotations

import threading

import pytest

from app.worker_periodic_heartbeat import start_periodic_heartbeats


pytestmark = pytest.mark.no_postgres


def test_local_heartbeat_continues_while_database_refresh_is_blocked() -> None:
    database_started = threading.Event()
    release_database = threading.Event()
    local_refreshed = threading.Event()

    def blocked_database_refresh() -> None:
        database_started.set()
        release_database.wait(timeout=1)

    stop_event, threads = start_periodic_heartbeats(
        database_refresh=blocked_database_refresh,
        local_refresh=local_refreshed.set,
        database_failure=lambda _code: None,
        local_failure=lambda _code: None,
        thread_name_prefix="test",
        interval_seconds=0.01,
    )
    try:
        assert database_started.wait(timeout=0.5)
        assert local_refreshed.wait(timeout=0.5)
    finally:
        stop_event.set()
        release_database.set()
        threads.join(timeout=1)
