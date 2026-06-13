from __future__ import annotations

import os
import socket
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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
    values = {
        "id": str(uuid4()),
        "worker_id": worker_id,
        "process_type": process_type,
        "hostname": hostname,
        "pid": pid,
        "status": "active",
        "heartbeat_metadata": metadata or {},
        "started_at": now_value,
        "last_seen_at": now_value,
    }
    if _upsert_worker_heartbeat(session, values):
        heartbeat = session.scalar(select(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == worker_id))
        if heartbeat is None:
            raise RuntimeError(f"worker heartbeat upsert did not create row: {worker_id}")
        return heartbeat
    heartbeat = session.scalar(select(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == worker_id))
    if not heartbeat:
        heartbeat = WorkerHeartbeat(**values)
        session.add(heartbeat)
    else:
        heartbeat.process_type = process_type
        heartbeat.hostname = hostname
        heartbeat.pid = pid
        heartbeat.status = "active"
        heartbeat.heartbeat_metadata = metadata or {}
        heartbeat.last_seen_at = now_value
    return heartbeat


def _upsert_worker_heartbeat(session: Session, values: dict) -> bool:
    dialect = session.bind.dialect.name if session.bind else ""
    insert_factory = {"postgresql": postgresql_insert, "sqlite": sqlite_insert}.get(dialect)
    if not insert_factory:
        return False
    table = WorkerHeartbeat.__table__
    statement = insert_factory(table).values(**values).on_conflict_do_update(
        index_elements=[table.c.worker_id],
        set_={
            "process_type": values["process_type"],
            "hostname": values["hostname"],
            "pid": values["pid"],
            "status": values["status"],
            "heartbeat_metadata": values["heartbeat_metadata"],
            "last_seen_at": values["last_seen_at"],
        },
    )
    session.execute(statement)
    return True


__all__ = ["record_worker_heartbeat", "worker_identity"]
