from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import socket
import threading
import time
from uuid import uuid4

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import WorkerRuntimeResourceSample
from app.services._common import _now
from app.telethon_lifecycle import TelethonClientLifecycle


SMAPS_FIELDS = {
    "Rss": "rss_kib",
    "Pss": "pss_kib",
    "Private_Dirty": "private_dirty_kib",
    "Anonymous": "anonymous_kib",
    "AnonHugePages": "anon_huge_pages_kib",
}
V1_ROOTS = (Path("/sys/fs/cgroup/memory"), Path("/sys/fs/cgroup"))
_sample_lock = threading.Lock()
_last_sample_at = 0.0
_previous_cpu: tuple[float, int] | None = None


@dataclass(frozen=True)
class CgroupSample:
    version: int = 0
    current_bytes: int = 0
    peak_bytes: int = 0
    limit_bytes: int = 0
    event_count: int = 0


def record_planner_resource_sample_if_due(
    session: Session,
    *,
    process_type: str,
    drain_metrics: dict,
) -> bool:
    if process_type != "planner" or not _claim_sample_interval():
        return False
    process = read_smaps_rollup(Path("/proc/self/smaps_rollup"))
    cgroup = read_cgroup_memory()
    sample = WorkerRuntimeResourceSample(
        id=str(uuid4()),
        worker_id_hash=_worker_id_hash(),
        process_type=process_type,
        release_sha=str(os.getenv("RELEASE_SHA") or os.getenv("GIT_SHA") or "")[:40],
        captured_at=_now(),
        sample_interval_seconds=_sample_interval_seconds(),
        cgroup_version=cgroup.version,
        **process,
        cgroup_current_bytes=cgroup.current_bytes,
        cgroup_peak_bytes=cgroup.peak_bytes,
        cgroup_limit_bytes=cgroup.limit_bytes,
        cgroup_event_count=cgroup.event_count,
        cpu_percent=_cpu_percent(),
        thread_count=_thread_count(),
        telethon_client_count=TelethonClientLifecycle.connected_client_count(),
        drain_metrics=drain_metrics,
        state="fresh" if process.get("pss_kib", 0) > 0 else "unsupported",
    )
    session.add(sample)
    return True


def read_smaps_rollup(path: Path) -> dict[str, int]:
    values = {field: 0 for field in SMAPS_FIELDS.values()}
    if not path.exists():
        return values
    for line in path.read_text(encoding="ascii").splitlines():
        name, separator, raw_value = line.partition(":")
        target = SMAPS_FIELDS.get(name)
        if not separator or target is None:
            continue
        values[target] = _first_int(raw_value)
    return values


def read_cgroup_memory(root: Path = Path("/sys/fs/cgroup")) -> CgroupSample:
    if (root / "cgroup.controllers").exists():
        return _read_cgroup_v2(root)
    for candidate in _v1_roots(root):
        if (candidate / "memory.usage_in_bytes").exists():
            return _read_cgroup_v1(candidate)
    return CgroupSample()


def _read_cgroup_v2(root: Path) -> CgroupSample:
    return CgroupSample(
        version=2,
        current_bytes=_read_int(root / "memory.current"),
        peak_bytes=_read_int(root / "memory.peak"),
        limit_bytes=_read_int(root / "memory.max"),
        event_count=_event_total(root / "memory.events"),
    )


def _read_cgroup_v1(root: Path) -> CgroupSample:
    return CgroupSample(
        version=1,
        current_bytes=_read_int(root / "memory.usage_in_bytes"),
        peak_bytes=_read_int(root / "memory.max_usage_in_bytes"),
        limit_bytes=_read_int(root / "memory.limit_in_bytes"),
        event_count=_read_int(root / "memory.failcnt"),
    )


def _v1_roots(root: Path) -> tuple[Path, ...]:
    if root != Path("/sys/fs/cgroup"):
        return (root / "memory", root)
    return V1_ROOTS


def _event_total(path: Path) -> int:
    if not path.exists():
        return 0
    values = (
        _first_int(line.partition(" ")[2])
        for line in path.read_text(encoding="ascii").splitlines()
    )
    return sum(values)


def _read_int(path: Path) -> int:
    if not path.exists():
        return 0
    raw = path.read_text(encoding="ascii").strip()
    return int(raw) if raw.isdigit() else 0


def _first_int(value: str) -> int:
    token = value.strip().split(maxsplit=1)[0] if value.strip() else ""
    return int(token) if token.isdigit() else 0


def _claim_sample_interval() -> bool:
    global _last_sample_at
    with _sample_lock:
        current = time.monotonic()
        if current - _last_sample_at < _sample_interval_seconds():
            return False
        _last_sample_at = current
        return True


def _sample_interval_seconds() -> int:
    return max(1, int(get_settings().planner_resource_sample_interval_seconds or 1))


def _cpu_percent() -> float:
    global _previous_cpu
    current = (time.monotonic(), _process_cpu_ticks())
    previous = _previous_cpu
    _previous_cpu = current
    if previous is None or current[0] <= previous[0]:
        return 0.0
    ticks_per_second = max(1, int(os.sysconf("SC_CLK_TCK")))
    cpu_seconds = max(0, current[1] - previous[1]) / ticks_per_second
    return round(cpu_seconds * 100 / (current[0] - previous[0]), 2)


def _process_cpu_ticks(path: Path = Path("/proc/self/stat")) -> int:
    if not path.exists():
        return 0
    fields = path.read_text(encoding="ascii").split()
    return int(fields[13]) + int(fields[14]) if len(fields) > 14 else 0


def _thread_count(path: Path = Path("/proc/self/status")) -> int:
    if not path.exists():
        return threading.active_count()
    for line in path.read_text(encoding="ascii").splitlines():
        if line.startswith("Threads:"):
            return _first_int(line.partition(":")[2])
    return 0


def _worker_id_hash() -> str:
    identity = f"{socket.gethostname()}:{os.getpid()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


__all__ = [
    "CgroupSample",
    "read_cgroup_memory",
    "read_smaps_rollup",
    "record_planner_resource_sample_if_due",
]
