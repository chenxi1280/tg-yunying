from __future__ import annotations

from threading import Event, Thread

from sqlalchemy.orm import Session

from app.models import WorkerHeartbeat
from app.services._common import _now

SOLVER_HEARTBEAT_RENEW_SECONDS = 15


class SolverLeaseRenewal:
    def __init__(self, bind, heartbeat_id: str, fencing_token: str):
        self._bind = bind
        self._heartbeat_id = heartbeat_id
        self._fencing_token = fencing_token
        self._stop = Event()
        self._thread: Thread | None = None

    def __enter__(self) -> "SolverLeaseRenewal":
        if self._bind.dialect.name != "sqlite":
            self._thread = Thread(target=self._renew_loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def _renew_loop(self) -> None:
        while not self._stop.wait(SOLVER_HEARTBEAT_RENEW_SECONDS):
            if not self._renew_once():
                return

    def _renew_once(self) -> bool:
        with Session(self._bind) as session:
            heartbeat = session.get(WorkerHeartbeat, self._heartbeat_id)
            metadata = heartbeat.heartbeat_metadata if heartbeat else {}
            if (
                heartbeat is None
                or heartbeat.status != "active"
                or metadata.get("search_solver_fencing_token")
                != self._fencing_token
            ):
                return False
            heartbeat.last_seen_at = _now()
            session.commit()
            return True


__all__ = ["SOLVER_HEARTBEAT_RENEW_SECONDS", "SolverLeaseRenewal"]
