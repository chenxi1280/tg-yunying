from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import threading


HeartbeatCallback = Callable[[], None]
FailureCallback = Callable[[Exception], None]


@dataclass(frozen=True)
class PeriodicHeartbeatThreads:
    threads: tuple[threading.Thread, ...]

    def join(self, timeout: int | float | None = None) -> None:
        for thread in self.threads:
            thread.join(timeout=timeout)


def start_periodic_heartbeats(
    *,
    database_refresh: HeartbeatCallback,
    local_refresh: HeartbeatCallback,
    database_failure: FailureCallback,
    local_failure: FailureCallback,
    thread_name_prefix: str,
    interval_seconds: float = 30,
) -> tuple[threading.Event, PeriodicHeartbeatThreads]:
    stop_event = threading.Event()
    threads = (
        _heartbeat_thread(
            stop_event,
            refresh=local_refresh,
            on_failure=local_failure,
            interval_seconds=interval_seconds,
            name=f"{thread_name_prefix}-local-heartbeat",
        ),
        _heartbeat_thread(
            stop_event,
            refresh=database_refresh,
            on_failure=database_failure,
            interval_seconds=interval_seconds,
            name=f"{thread_name_prefix}-database-heartbeat",
        ),
    )
    for thread in threads:
        thread.start()
    return stop_event, PeriodicHeartbeatThreads(threads)


def _heartbeat_thread(
    stop_event: threading.Event,
    *,
    refresh: HeartbeatCallback,
    on_failure: FailureCallback,
    interval_seconds: float,
    name: str,
) -> threading.Thread:
    return threading.Thread(
        target=_periodic_refresh_loop,
        args=(stop_event,),
        kwargs={
            "refresh": refresh,
            "on_failure": on_failure,
            "interval_seconds": interval_seconds,
        },
        name=name,
        daemon=True,
    )


def _periodic_refresh_loop(
    stop_event: threading.Event,
    *,
    refresh: HeartbeatCallback,
    on_failure: FailureCallback,
    interval_seconds: float,
) -> None:
    while not stop_event.wait(interval_seconds):
        try:
            refresh()
        except Exception as exc:  # noqa: BLE001 - health failures stay visible and retry.
            on_failure(exc)


__all__ = ["PeriodicHeartbeatThreads", "start_periodic_heartbeats"]
