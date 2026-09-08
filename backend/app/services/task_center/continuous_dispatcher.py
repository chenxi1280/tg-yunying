"""Keep real executor occupancy across worker ticks, with per-action ownership."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Event
from typing import Callable, Iterable

from .runtime_resources import (
    dispatch_runtime_reservation_scope, release_transferred_dispatch_reservations,
    transfer_dispatch_reservations,
)


_ACTIVE_DISPATCHER = ContextVar("continuous_dispatcher", default=None)


@dataclass(frozen=True)
class DispatcherCallbacks:
    claim: Callable[[int], Iterable[str]]
    dispatch: Callable[[str], int]


class ContinuousDispatcher:
    def __init__(self):
        self._executor = None
        self._capacity = None
        self._futures = {}
        self.completed = Event()

    def drain(self, callbacks, *, capacity, limit):
        self._configure(capacity)
        processed = self._reap()
        free = min(limit, self._capacity - len(self._futures))
        if free <= 0:
            return processed
        with dispatch_runtime_reservation_scope():
            identifiers = tuple(callbacks.claim(free))
            if len(identifiers) > free:
                raise RuntimeError("dispatcher_claim_exceeds_free_capacity")
            for action_id in identifiers:
                self._submit(action_id, callbacks.dispatch)
        return processed

    def _configure(self, capacity):
        if self._executor is None:
            self._capacity = capacity
            self._executor = ThreadPoolExecutor(
                max_workers=capacity, thread_name_prefix="task-dispatcher",
            )
        if capacity != self._capacity:
            raise RuntimeError("dispatcher_runtime_capacity_changed")

    def _submit(self, action_id, dispatch):
        if action_id in self._futures:
            raise RuntimeError("dispatcher_action_already_registered")
        captured = transfer_dispatch_reservations((action_id,))
        try:
            future = self._executor.submit(_execute, action_id, dispatch, captured)
        except BaseException:
            release_transferred_dispatch_reservations(captured)
            raise
        self._futures[action_id] = future
        future.add_done_callback(lambda _future: self.completed.set())

    def _reap(self):
        self.completed.clear()
        processed, errors = 0, []
        for action_id, future in tuple(self._futures.items()):
            if not future.done():
                continue
            del self._futures[action_id]
            try:
                processed += int(future.result() or 0)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("dispatcher_action_execution_failed", errors)
        return processed

    def close(self):
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._reap()


def _execute(action_id, dispatch, captured):
    try:
        return dispatch(action_id)
    finally:
        release_transferred_dispatch_reservations(captured)


def active_dispatcher():
    return _ACTIVE_DISPATCHER.get()


@contextmanager
def continuous_dispatcher_scope(*, enabled):
    if not enabled:
        yield
        return
    dispatcher = ContinuousDispatcher()
    token = _ACTIVE_DISPATCHER.set(dispatcher)
    try:
        yield
    finally:
        _ACTIVE_DISPATCHER.reset(token)
        dispatcher.close()
