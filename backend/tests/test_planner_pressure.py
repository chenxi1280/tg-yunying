from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import WorkerRuntimeResourceSample
from app.services.task_center import planner_pressure


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 8, 17, 15, 0)


def test_planner_pressure_reports_latest_memory_and_drain_percentiles(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(planner_pressure, "_now", lambda: NOW)
    with Session(engine) as session:
        for index, took_ms in enumerate((100, 200, 400, 800)):
            session.add(WorkerRuntimeResourceSample(
                worker_id_hash="a" * 64,
                process_type="planner",
                release_sha="f" * 40,
                captured_at=NOW - timedelta(seconds=3 - index),
                sample_interval_seconds=10,
                cgroup_version=1,
                rss_kib=500_000 + index,
                pss_kib=400_000 + index,
                private_dirty_kib=390_000 + index,
                anonymous_kib=380_000 + index,
                cpu_percent=12.5,
                thread_count=3,
                telethon_client_count=0,
                drain_metrics={"took_ms": took_ms, "processed_count": index},
            ))
        session.commit()

        payload = planner_pressure.planner_pressure_payload(session)

        assert payload["state"] == "fresh"
        assert payload["memory_kib"]["pss"] == 400_003
        assert payload["drain"] == {
            "p50_ms": 200,
            "p95_ms": 800,
            "latest_processed_count": 3,
            "sample_count": 4,
        }
        assert payload["telethon_client_count"] == 0


def test_planner_pressure_distinguishes_stale_and_degraded(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(planner_pressure, "_now", lambda: NOW)
    with Session(engine) as session:
        sample = WorkerRuntimeResourceSample(
            worker_id_hash="a" * 64,
            process_type="planner",
            captured_at=NOW - timedelta(minutes=1),
            sample_interval_seconds=10,
        )
        session.add(sample)
        session.commit()
        assert planner_pressure.planner_pressure_payload(session)["state"] == "stale"

        sample.captured_at = NOW
        sample.state = "degraded"
        session.commit()
        assert planner_pressure.planner_pressure_payload(session)["state"] == "degraded"
