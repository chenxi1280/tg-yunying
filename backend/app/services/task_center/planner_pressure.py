from __future__ import annotations

from datetime import datetime, timedelta
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import WorkerRuntimeResourceSample
from app.services._common import _now


PRESSURE_SAMPLE_LIMIT = 720


def planner_pressure_payload(session: Session) -> dict:
    rows = list(session.scalars(
        select(WorkerRuntimeResourceSample)
        .where(WorkerRuntimeResourceSample.process_type == "planner")
        .order_by(WorkerRuntimeResourceSample.captured_at.desc())
        .limit(PRESSURE_SAMPLE_LIMIT)
    ))
    if not rows:
        return {"version": "planner_pressure_v1", "state": "unavailable"}
    latest = rows[0]
    freshness = timedelta(seconds=max(1, latest.sample_interval_seconds) * 3)
    state = "fresh" if _wall(latest.captured_at) + freshness >= _wall(_now()) else "stale"
    return {
        "version": "planner_pressure_v1",
        "state": state if latest.state == "fresh" else "degraded",
        "captured_at": latest.captured_at,
        "sample_interval_seconds": latest.sample_interval_seconds,
        "release_sha": latest.release_sha,
        "worker_id_hash": latest.worker_id_hash,
        "memory_kib": {
            "rss": latest.rss_kib,
            "pss": latest.pss_kib,
            "private_dirty": latest.private_dirty_kib,
            "anonymous": latest.anonymous_kib,
            "anon_huge_pages": latest.anon_huge_pages_kib,
        },
        "cgroup": {
            "version": latest.cgroup_version,
            "current_bytes": latest.cgroup_current_bytes,
            "peak_bytes": latest.cgroup_peak_bytes,
            "limit_bytes": latest.cgroup_limit_bytes,
            "event_count": latest.cgroup_event_count,
        },
        "cpu_percent": latest.cpu_percent,
        "thread_count": latest.thread_count,
        "telethon_client_count": latest.telethon_client_count,
        "drain": _drain_summary(rows),
    }


def _drain_summary(rows: list[WorkerRuntimeResourceSample]) -> dict:
    drain_ms = sorted(
        int(row.drain_metrics.get("took_ms") or 0)
        for row in rows
        if isinstance(row.drain_metrics, dict)
    )
    processed = [
        int(row.drain_metrics.get("processed_count") or 0)
        for row in rows
        if isinstance(row.drain_metrics, dict)
    ]
    return {
        "p50_ms": _percentile(drain_ms, 0.50),
        "p95_ms": _percentile(drain_ms, 0.95),
        "latest_processed_count": processed[0] if processed else 0,
        "sample_count": len(rows),
    }


def _percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    index = min(len(values) - 1, max(0, math.ceil(len(values) * ratio) - 1))
    return values[index]


def _wall(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


__all__ = ["planner_pressure_payload"]
