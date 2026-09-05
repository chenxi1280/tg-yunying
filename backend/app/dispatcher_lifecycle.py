from __future__ import annotations

import logging
import os
import resource
import socket
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic
from typing import Callable
from uuid import uuid4

from sqlalchemy import func, or_, select

from app.dispatcher_recycle_lease import RecycleLease, RedisRecycleLease
from app.models import Action, ExecutionAttempt
from app.services.image_verification_runtime import ImageVerificationRuntime

logger = logging.getLogger(__name__)

DRAIN_POLL_SECONDS = 0.1
CGROUP_MEMORY_CURRENT_PATH = Path("/sys/fs/cgroup/memory.current")
PROC_STATUS_PATH = Path("/proc/self/status")
RUNNING_ATTEMPT_STATUSES = frozenset(
    {"before_call", "before_gateway", "gateway_call_started"}
)


@dataclass(frozen=True)
class DispatcherMetrics:
    rss_bytes: int
    cgroup_bytes: int
    uptime_seconds: float
    ocr_attempts: int


@dataclass(frozen=True)
class DispatcherSafetySnapshot:
    active_operations: int
    owned_actions: int
    unfinished_attempts: int
    gateway_open: bool
    runtime_reservations: int = 0
    probe_error: str = ""

    def blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        for name, value in (
            ("active_operations", self.active_operations),
            ("owned_actions", self.owned_actions),
            ("unfinished_attempts", self.unfinished_attempts),
            ("runtime_reservations", self.runtime_reservations),
        ):
            if value:
                blockers.append(name)
        if self.gateway_open:
            blockers.append("gateway_open")
        if self.probe_error:
            blockers.append("safety_probe_error")
        return tuple(blockers)


class DispatcherSafetyProbe:
    def __init__(
        self,
        session_factory: Callable,
        worker_id: str,
        active_count: Callable[[], int],
        reservation_count: Callable[[], int] = lambda: 0,
    ) -> None:
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._active_count = active_count
        self._reservation_count = reservation_count

    def snapshot(self, *, gateway_open: bool) -> DispatcherSafetySnapshot:
        try:
            with self._session_factory() as session:
                owned_actions = session.scalar(
                    select(func.count(Action.id)).where(
                        or_(
                            Action.claim_owner == self._worker_id,
                            Action.lease_owner == self._worker_id,
                        ),
                        Action.status.in_(("claiming", "executing")),
                    )
                )
                attempts = session.scalar(
                    select(func.count(ExecutionAttempt.id)).where(
                        ExecutionAttempt.worker_id == self._worker_id,
                        ExecutionAttempt.status.in_(RUNNING_ATTEMPT_STATUSES),
                    )
                )
            return DispatcherSafetySnapshot(
                active_operations=self._active_count(),
                owned_actions=int(owned_actions or 0),
                unfinished_attempts=int(attempts or 0),
                gateway_open=gateway_open,
                runtime_reservations=self._reservation_count(),
            )
        except Exception as exc:  # noqa: BLE001 - DB outage blocks safe exit.
            return DispatcherSafetySnapshot(
                active_operations=self._active_count(),
                owned_actions=0,
                unfinished_attempts=0,
                gateway_open=gateway_open,
                runtime_reservations=self._reservation_count(),
                probe_error=exc.__class__.__name__,
            )


class DispatcherLifecycle:
    def __init__(
        self,
        *,
        settings: object,
        safety_probe: DispatcherSafetyProbe,
        lease: RecycleLease,
        disconnect_gateway: Callable[[float], int],
        metrics_reader: Callable[[], DispatcherMetrics],
        instance_id: str = "",
        shard_index: int = 0,
    ) -> None:
        self._settings = settings
        self._safety_probe = safety_probe
        self._lease = lease
        self._disconnect_gateway = disconnect_gateway
        self._metrics_reader = metrics_reader
        self._lock = threading.Lock()
        self.state = "active"
        self.trigger = ""
        self.automatic = False
        self.gateway_open = True
        self._has_recycle_lease = False
        self._waiting_recycle_trigger = ""
        self._successor_checked = False
        self.instance_id = instance_id or str(uuid4())
        self.shard_index = shard_index

    def request_stop(self, trigger: str, *, automatic: bool) -> None:
        if self.state != "active":
            return
        if automatic and not self._lease.acquire():
            if self._waiting_recycle_trigger != trigger:
                logger.warning(
                    "dispatcher lifecycle recycle deferred trigger=%s "
                    "reason=recycle_lease_unavailable state=active",
                    trigger,
                )
                self._waiting_recycle_trigger = trigger
            return
        with self._lock:
            accepted = self.state == "active"
            if accepted:
                self._has_recycle_lease = automatic
                self._waiting_recycle_trigger = ""
                self.state = "recycle_requested"
                self.trigger = trigger
                self.automatic = automatic
        if not accepted:
            if automatic and not self._lease.release():
                logger.warning("dispatcher lifecycle unused recycle lease release failed")
            return
        logger.info("dispatcher lifecycle recycle_requested trigger=%s", trigger)

    def observe_after_batch(self) -> None:
        if self.state != "active" or not self._settings.dispatcher_recycle_enabled:
            return
        metrics = self._metrics_reader()
        trigger = _recycle_trigger(self._settings, metrics)
        if trigger:
            self.request_stop(trigger, automatic=True)

    def acknowledge_successor(self) -> bool:
        if self.state != "active" or self._successor_checked:
            return False
        released = self._lease.acknowledge_successor()
        if released is None:
            logger.warning("dispatcher lifecycle successor lease ack failed")
            return False
        self._successor_checked = True
        if released:
            logger.info("dispatcher lifecycle predecessor lease released")
        return released

    def drain_until_safe(
        self,
        heartbeat: Callable[[dict[str, object]], None],
        stop_event: threading.Event | None = None,
    ) -> DispatcherSafetySnapshot:
        self._acquire_automatic_lease(heartbeat, stop_event)
        self.state = "draining"
        while True:
            if self.automatic and not self._lease.renew():
                self._has_recycle_lease = False
                self.state = "drain_blocked"
                self._acquire_automatic_lease(
                    heartbeat,
                    stop_event,
                )
                self.state = "draining"
            snapshot = self._safety_probe.snapshot(
                gateway_open=self.gateway_open
            )
            if snapshot.blockers() == ("gateway_open",):
                try:
                    self._close_gateway()
                except Exception as exc:  # noqa: BLE001 - disconnect blocks exit.
                    self.state = "drain_blocked"
                    _persist_heartbeat(
                        heartbeat,
                        self.metadata(
                            snapshot,
                            blocker=(
                                "gateway_disconnect_failed:"
                                f"{exc.__class__.__name__}"
                            ),
                        )
                    )
                    _wait(stop_event)
                continue
            if not snapshot.blockers():
                self.state = "safe_to_exit"
                metadata = self.metadata(snapshot)
                if not _persist_heartbeat(heartbeat, metadata):
                    self.state = "drain_blocked"
                    _wait(stop_event)
                    continue
                logger.info(
                    "dispatcher lifecycle safe_to_exit metadata=%s",
                    metadata,
                )
                return snapshot
            _persist_heartbeat(heartbeat, self.metadata(snapshot))
            _wait(stop_event)

    def _acquire_automatic_lease(
        self,
        heartbeat: Callable[[dict[str, object]], None],
        stop_event: threading.Event | None,
    ) -> bool:
        if not self.automatic or self._has_recycle_lease:
            return False
        while not self._lease.acquire():
            self.state = "drain_blocked"
            _persist_heartbeat(
                heartbeat,
                self.metadata(blocker="recycle_lease_unavailable"),
            )
            _wait(stop_event)
        self._has_recycle_lease = True
        return True

    def _close_gateway(self) -> None:
        self._disconnect_gateway(
            self._settings.dispatcher_gateway_shutdown_timeout_seconds
        )
        self.gateway_open = False

    def metadata(
        self,
        snapshot: DispatcherSafetySnapshot | None = None,
        *,
        blocker: str = "",
    ) -> dict[str, object]:
        metrics = self._metrics_reader()
        return {
            "lifecycle_state": self.state,
            "worker_instance_id": self.instance_id,
            "shard_index": self.shard_index,
            "recycle_trigger": self.trigger,
            "automatic_recycle": self.automatic,
            "drain_blocker": blocker,
            "metrics": asdict(metrics),
            "safety": asdict(snapshot) if snapshot else {},
        }


def create_dispatcher_lifecycle(
    settings: object,
    session_factory: Callable,
    runtime: ImageVerificationRuntime | None,
    disconnect_gateway: Callable[[float], int],
    reservation_count: Callable[[], int] = lambda: 0,
) -> DispatcherLifecycle:
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    instance_id = str(uuid4())
    active_count = runtime.registry.count if runtime else lambda: 0
    client = _redis_client(settings.redis_url)
    lease = RedisRecycleLease(
        client,
        worker_instance_id=instance_id,
        shard_index=int(settings.account_shard_index),
        ttl_seconds=int(settings.dispatcher_recycle_lease_seconds or 1),
    )
    started = monotonic()
    metrics_reader = lambda: _dispatcher_metrics(runtime, started)
    return DispatcherLifecycle(
        settings=settings,
        safety_probe=DispatcherSafetyProbe(
            session_factory,
            worker_id,
            active_count,
            reservation_count,
        ),
        lease=lease,
        disconnect_gateway=disconnect_gateway,
        metrics_reader=metrics_reader,
        instance_id=instance_id,
        shard_index=int(settings.account_shard_index),
    )


def _recycle_trigger(
    settings: object,
    metrics: DispatcherMetrics,
) -> str:
    thresholds = (
        ("rss", metrics.rss_bytes, settings.dispatcher_recycle_soft_rss_bytes),
        ("cgroup", metrics.cgroup_bytes, settings.dispatcher_recycle_soft_cgroup_bytes),
        ("ocr_attempts", metrics.ocr_attempts, settings.dispatcher_recycle_ocr_attempt_limit),
        ("uptime", metrics.uptime_seconds, settings.dispatcher_recycle_max_uptime_seconds),
    )
    return next(
        (name for name, value, threshold in thresholds if threshold > 0 and value >= threshold),
        "",
    )


def _dispatcher_metrics(
    runtime: ImageVerificationRuntime | None,
    started: float,
) -> DispatcherMetrics:
    return DispatcherMetrics(
        rss_bytes=_current_rss_bytes(),
        cgroup_bytes=_cgroup_memory_bytes(),
        uptime_seconds=max(0.0, monotonic() - started),
        ocr_attempts=runtime.challenge_count() if runtime else 0,
    )


def _current_rss_bytes() -> int:
    try:
        for line in PROC_STATUS_PATH.read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(usage if os.uname().sysname == "Darwin" else usage * 1024)


def _cgroup_memory_bytes() -> int:
    try:
        return int(CGROUP_MEMORY_CURRENT_PATH.read_text().strip())
    except (OSError, ValueError):
        return 0


def _redis_client(redis_url: str):
    import redis

    return redis.Redis.from_url(
        redis_url,
        socket_connect_timeout=1,
        socket_timeout=1,
    )


def _wait(stop_event: threading.Event | None) -> None:
    if stop_event is not None and not stop_event.is_set():
        stop_event.wait(DRAIN_POLL_SECONDS)
        return
    threading.Event().wait(DRAIN_POLL_SECONDS)


def _persist_heartbeat(
    heartbeat: Callable[[dict[str, object]], None],
    metadata: dict[str, object],
) -> bool:
    try:
        heartbeat(metadata)
        return True
    except Exception:  # noqa: BLE001 - failed persistence blocks safe exit.
        logger.exception("dispatcher lifecycle heartbeat persistence failed")
        return False
