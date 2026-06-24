from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import WorkerHeartbeat
from app.services.task_center.heartbeat import record_worker_heartbeat


def test_drain_once_dispatches_task_center_roles(monkeypatch):
    from app import worker

    calls: list[tuple[str, int]] = []

    monkeypatch.setattr(worker, "drain_task_planner", lambda _factory, limit: calls.append(("planner", limit)) or 1)
    monkeypatch.setattr(worker, "drain_task_dispatcher", lambda _factory, limit: calls.append(("dispatcher", limit)) or 2)
    monkeypatch.setattr(worker, "drain_task_listener", lambda _factory, limit: calls.append(("listener", limit)) or 3)
    monkeypatch.setattr(worker, "drain_task_recovery", lambda _factory, limit: calls.append(("recovery", limit)) or 4)
    monkeypatch.setattr(worker, "drain_account_security_batches", lambda _factory, limit: calls.append(("account_security", limit)) or 6)
    monkeypatch.setattr(worker, "drain_material_cache", lambda _factory, limit: calls.append(("material_cache", limit)) or 1)
    monkeypatch.setattr(worker, "drain_task_metrics", lambda _factory, limit: calls.append(("metrics", limit)) or 5)

    assert worker.drain_once(7, role="planner") == 1
    assert worker.drain_once(7, role="dispatcher") == 2
    assert worker.drain_once(7, role="listener") == 3
    assert worker.drain_once(7, role="recovery") == 4
    assert worker.drain_once(7, role="account-security") == 7
    assert worker.drain_once(7, role="material-cache") == 1
    assert worker.drain_once(7, role="metrics") == 5

    assert calls == [
        ("planner", 7),
        ("dispatcher", 7),
        ("listener", 7),
        ("recovery", 7),
        ("material_cache", 7),
        ("account_security", 6),
        ("material_cache", 7),
        ("metrics", 7),
    ]


def test_drain_once_uses_worker_role_from_settings(monkeypatch):
    from app import worker

    calls: list[str] = []
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(worker_role="dispatcher"))
    monkeypatch.setattr(worker, "drain_task_dispatcher", lambda *_args: calls.append("dispatcher") or 9)

    assert worker.drain_once(3) == 9
    assert calls == ["dispatcher"]


def test_drain_once_all_keeps_legacy_and_task_center_compatibility(monkeypatch):
    from app import worker

    calls: list[str] = []
    monkeypatch.setattr(worker, "_drain_legacy_once", lambda limit: calls.append(f"legacy:{limit}") or 4)
    monkeypatch.setattr(worker, "drain_task_center", lambda _factory, limit: calls.append(f"task_center:{limit}") or 6)

    assert worker.drain_once(5, role="all") == 10
    assert calls == ["legacy:5", "task_center:5"]


def test_worker_role_rejects_unknown_role():
    from app import worker

    with pytest.raises(ValueError, match="unsupported worker role"):
        worker.drain_once(role="everything")


def test_worker_main_once_accepts_role(monkeypatch, capsys):
    from app import worker

    calls: list[tuple[int, str | None]] = []
    monkeypatch.setattr(worker, "drain_once", lambda limit=100, *, role=None: calls.append((limit, role)) or 8)

    assert worker.main(["--once", "--role", "metrics", "--limit", "2"]) == 0
    assert calls == [(2, "metrics")]
    out = capsys.readouterr().out
    assert "role=metrics" in out
    assert "processed=8" in out


def test_server_compose_metrics_worker_uses_dedicated_interval():
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.server.yml"
    compose = compose_path.read_text()
    metrics_block = compose.split("worker-metrics:", 1)[1].split("networks:", 1)[0]

    assert "METRICS_WORKER_INTERVAL_SECONDS" in metrics_block
    assert "${WORKER_INTERVAL_SECONDS:-10.0}" not in metrics_block


def test_worker_main_healthcheck_uses_role_heartbeat(monkeypatch):
    from app import worker

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            WorkerHeartbeat(
                worker_id="pytest-planner",
                process_type="planner",
                status="active",
                last_seen_at=worker._now(),
            )
        )
        session.commit()

    monkeypatch.setattr(worker, "SessionLocal", lambda: Session(engine))

    assert worker.main(["--healthcheck", "--role", "planner"]) == 0
    assert worker.main(["--healthcheck", "--role", "dispatcher"]) == 1


def test_worker_main_healthcheck_fails_for_stale_role(monkeypatch):
    from app import worker

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            WorkerHeartbeat(
                worker_id="pytest-stale",
                process_type="listener",
                status="active",
                last_seen_at=worker._now() - worker.WORKER_HEALTH_STALE_AFTER - timedelta(seconds=1),
            )
        )
        session.commit()

    monkeypatch.setattr(worker, "SessionLocal", lambda: Session(engine))

    assert worker.main(["--healthcheck", "--role", "listener"]) == 1


def test_explicit_worker_id_is_scoped_by_process_type(monkeypatch):
    monkeypatch.setenv("TG_OPS_WORKER_ID", "pytest-worker")

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        record_worker_heartbeat(session, process_type="planner")
        record_worker_heartbeat(session, process_type="dispatcher")
        session.commit()
        heartbeats = session.query(WorkerHeartbeat).order_by(WorkerHeartbeat.process_type).all()

    assert [heartbeat.worker_id for heartbeat in heartbeats] == [
        "pytest-worker:dispatcher",
        "pytest-worker:planner",
    ]
    assert [heartbeat.process_type for heartbeat in heartbeats] == ["dispatcher", "planner"]


def test_worker_heartbeat_updates_existing_worker_id(monkeypatch):
    monkeypatch.setenv("TG_OPS_WORKER_ID", "pytest-worker")

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        record_worker_heartbeat(session, process_type="planner", metadata={"limit": 10})
        session.commit()
        first = session.query(WorkerHeartbeat).one()
        first_seen_at = first.last_seen_at

        record_worker_heartbeat(session, process_type="planner", metadata={"limit": 20})
        session.commit()
        heartbeats = session.query(WorkerHeartbeat).all()

    assert len(heartbeats) == 1
    assert heartbeats[0].worker_id == "pytest-worker:planner"
    assert heartbeats[0].heartbeat_metadata == {"limit": 20}
    assert heartbeats[0].last_seen_at >= first_seen_at


def test_worker_health_module_checks_role_heartbeat_without_worker_imports(monkeypatch):
    from app import worker_health

    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE worker_heartbeats (
                worker_id VARCHAR(160),
                process_type VARCHAR(60),
                status VARCHAR(30),
                last_seen_at DATETIME
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO worker_heartbeats VALUES (?, ?, ?, ?)",
            ("pytest-dispatcher", "dispatcher", "active", worker_health.beijing_now()),
        )

    monkeypatch.setattr(worker_health, "SessionLocal", lambda: Session(engine))

    assert worker_health.main(["--role", "dispatcher"]) == 0
    assert worker_health.main(["--role", "planner"]) == 1
