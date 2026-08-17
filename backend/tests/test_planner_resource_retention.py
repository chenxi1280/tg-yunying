from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import WorkerRuntimeResourceRollup, WorkerRuntimeResourceSample
from app.services.task_center.runtime_retention import (
    cleanup_runtime_metric_snapshots,
    rollup_worker_runtime_resources,
)


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 8, 18, 12, 5, tzinfo=timezone.utc)


def test_resource_samples_roll_up_by_completed_five_minute_bucket() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            _sample("sample-1", NOW - timedelta(minutes=5), pss_kib=400_000),
            _sample("sample-2", NOW - timedelta(minutes=1), pss_kib=500_000),
        ])
        session.commit()

        assert rollup_worker_runtime_resources(session, now_value=NOW) == 1
        session.commit()
        rollup = session.scalar(select(WorkerRuntimeResourceRollup))

        assert rollup.bucket_at == (NOW - timedelta(minutes=5)).replace(tzinfo=None)
        assert rollup.sample_count == 2
        assert rollup.pss_kib_p95 == 500_000
        assert rollup.pss_kib_max == 500_000


def test_resource_retention_keeps_24_hour_raw_and_seven_day_rollup() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            _sample("raw-old", NOW - timedelta(hours=25), pss_kib=1),
            _sample("raw-fresh", NOW - timedelta(hours=23), pss_kib=2),
            _rollup("rollup-old", NOW - timedelta(days=8)),
            _rollup("rollup-fresh", NOW - timedelta(days=6)),
        ])
        session.commit()

        deleted = cleanup_runtime_metric_snapshots(
            session,
            now_value=NOW,
            today=NOW.date(),
        )
        session.commit()

        assert deleted == 2
        assert session.scalar(select(func.count(WorkerRuntimeResourceSample.id))) == 1
        assert session.scalar(select(func.count(WorkerRuntimeResourceRollup.id))) == 1


def _sample(sample_id: str, captured_at: datetime, *, pss_kib: int):
    return WorkerRuntimeResourceSample(
        id=sample_id,
        worker_id_hash="worker",
        process_type="planner",
        release_sha="release",
        captured_at=captured_at,
        pss_kib=pss_kib,
        private_dirty_kib=pss_kib - 1,
        anonymous_kib=pss_kib - 2,
        cgroup_current_bytes=pss_kib * 1024,
        cgroup_event_count=0,
        cpu_percent=10,
        thread_count=3,
        telethon_client_count=0,
    )


def _rollup(rollup_id: str, bucket_at: datetime):
    return WorkerRuntimeResourceRollup(
        id=rollup_id,
        worker_id_hash="worker",
        process_type="planner",
        release_sha="release",
        bucket_at=bucket_at,
        sample_count=1,
    )
