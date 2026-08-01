from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import worker
from app.config import Settings
from app.database import Base
from app.services.task_center import service
from app.services.task_center.heartbeat import (
    record_worker_heartbeat,
    retire_worker_heartbeat,
)


pytestmark = pytest.mark.no_postgres
ROOT = Path(__file__).resolve().parents[2]


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_worker_heartbeat_merges_contract_metadata_and_retires(monkeypatch) -> None:
    monkeypatch.setenv("TG_OPS_WORKER_ID", "planner-one")
    with _session() as session:
        first = record_worker_heartbeat(
            session, process_type="planner",
            metadata={"dispatch_contract_version": "dispatch-rebuild-v3"},
        )
        session.commit()
        record_worker_heartbeat(session, process_type="planner", metadata={"limit": 20})
        session.commit()
        session.refresh(first)
        assert first.heartbeat_metadata == {
            "dispatch_contract_version": "dispatch-rebuild-v3", "limit": 20,
        }

        assert retire_worker_heartbeat(session, process_type="planner") is True
        session.commit()
        session.refresh(first)
        assert first.status == "stopped"
        assert first.heartbeat_metadata["dispatch_contract_version"] == "dispatch-rebuild-v3"

        record_worker_heartbeat(
            session,
            process_type="planner",
            metadata={"limit": 21},
        )
        session.commit()
        session.refresh(first)
        assert first.status == "active"
        assert first.heartbeat_metadata["dispatch_contract_version"] == "dispatch-rebuild-v3"
        assert "stopped_at" not in first.heartbeat_metadata


def test_planner_task_heartbeat_keeps_worker_contract_metadata(monkeypatch) -> None:
    monkeypatch.setenv("TG_OPS_WORKER_ID", "planner-drain")
    with _session() as session:
        heartbeat = record_worker_heartbeat(
            session,
            process_type="planner",
            metadata={"dispatch_contract_version": "dispatch-rebuild-v3"},
        )
        session.commit()

        service._refresh_planner_heartbeat(
            session,
            "planner",
            20,
            task_id="task-1",
        )

        session.refresh(heartbeat)
        assert heartbeat.heartbeat_metadata == {
            "dispatch_contract_version": "dispatch-rebuild-v3",
            "limit": 20,
            "phase": "task",
            "task_id": "task-1",
        }


def test_run_worker_retires_its_database_heartbeat(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []

    class _Thread:
        def join(self, timeout: int) -> None:
            assert timeout == 1

    class _Stop:
        def set(self) -> None:
            return None

    monkeypatch.setattr(worker, "_start_periodic_heartbeat", lambda *_args: (_Stop(), _Thread()))
    monkeypatch.setattr(worker, "_record_loop_heartbeat", lambda *_args: None)
    monkeypatch.setattr(worker, "_write_local_healthcheck_heartbeat", lambda: None)
    monkeypatch.setattr(worker, "drain_once", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(worker, "_retire_loop_heartbeat", lambda role, limit: calls.append((role, limit)))

    worker.run_worker(limit=7, interval_seconds=0.1, max_iterations=1, role="planner")

    assert calls == [("planner", 7)]


def test_production_rejects_embedded_worker() -> None:
    with pytest.raises(ValueError, match="ENABLE_EMBEDDED_WORKER"):
        Settings(
            app_env="production",
            session_secret_key="secure-session-secret",
            admin_bootstrap_password="secure-admin-password",
            enable_embedded_worker=True,
        )


def test_release_retires_stopped_writers_before_stage() -> None:
    script = (ROOT / "deploy" / "compose-up.sh").read_text()
    assert script.index("retire-stopped-writers") < script.index("manage_shared_dispatch_contract stage")
    assert "workers_stopped_before=\"$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)\"" in script
    compose = (ROOT / "docker-compose.server.yml").read_text()
    for worker_name in ("planner", "ai-generation", "dispatcher-1", "dispatcher-2", "recovery"):
        assert f"TG_OPS_WORKER_ID: tgyunying-worker-{worker_name}" in compose
