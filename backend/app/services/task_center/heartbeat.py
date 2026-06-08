from __future__ import annotations

import os
import socket

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import WorkerHeartbeat
from app.services._common import _now


def worker_identity(process_type: str = "task_center") -> tuple[str, str, int]:
    hostname = socket.gethostname()
    pid = os.getpid()
    explicit = os.getenv("TG_OPS_WORKER_ID", "").strip()
    worker_id = f"{explicit}:{process_type}" if explicit else f"{hostname}:{pid}:{process_type}"
    return worker_id, hostname, pid


def record_worker_heartbeat(session: Session, *, process_type: str = "task_center", metadata: dict | None = None) -> WorkerHeartbeat:
    worker_id, hostname, pid = worker_identity(process_type)
    now_value = _now()
    heartbeat = session.scalar(select(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == worker_id))
    if not heartbeat:
        heartbeat = WorkerHeartbeat(
            worker_id=worker_id,
            process_type=process_type,
            hostname=hostname,
            pid=pid,
            status="active",
            heartbeat_metadata=metadata or {},
            started_at=now_value,
            last_seen_at=now_value,
        )
        session.add(heartbeat)
    else:
        heartbeat.process_type = process_type
        heartbeat.hostname = hostname
        heartbeat.pid = pid
        heartbeat.status = "active"
        heartbeat.heartbeat_metadata = metadata or {}
        heartbeat.last_seen_at = now_value
    return heartbeat


__all__ = ["record_worker_heartbeat", "worker_identity"]
