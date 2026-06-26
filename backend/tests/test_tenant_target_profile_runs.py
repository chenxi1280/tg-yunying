from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import OperationTarget, Tenant, TenantLearningProfile, TenantLearningRun, TenantLearningSource
from app.services.tenant_target_profile import recompute_candidates, rebuild_profile, start_source_run


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_source_sync_failure_writes_visible_failed_run() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-10031", title="活群"))
        source = TenantLearningSource(tenant_id=1, target_id=31, source_kind="group", is_enabled=False)
        session.add(source)
        session.commit()
        source_id = source.id

        with pytest.raises(ValueError, match="学习来源已停用"):
            start_source_run(session, 1, source_id, "sync", actor="tester")
        with pytest.raises(ValueError, match="学习来源已停用"):
            start_source_run(session, 1, source_id, "pull_history", actor="tester")

        runs = {
            run.run_type: {
                "status": run.status,
                "failure_detail": run.failure_detail,
                "trace_id": run.trace_id,
            }
            for run in session.scalars(select(TenantLearningRun).where(TenantLearningRun.source_id == source_id))
        }
        session.commit()

    assert set(runs) == {"sync", "pull_history"}
    assert runs["sync"]["status"] == "failed"
    assert runs["pull_history"]["status"] == "failed"
    assert "学习来源已停用" in runs["sync"]["failure_detail"]
    assert "学习来源已停用" in runs["pull_history"]["failure_detail"]
    assert runs["sync"]["trace_id"].startswith(f"sync-{source_id}")
    assert runs["pull_history"]["trace_id"].startswith(f"pull_history-{source_id}")


def test_recompute_failure_writes_visible_failed_run(monkeypatch) -> None:
    def broken_recompute(*_args, **_kwargs):
        raise RuntimeError("quality rule engine unavailable")

    monkeypatch.setattr("app.services.tenant_learning_samples.recompute_source_candidates", broken_recompute)

    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        with pytest.raises(ValueError, match="quality rule engine unavailable"):
            recompute_candidates(session, 1, actor="tester", reason="重算失败取证")

        run = session.scalar(select(TenantLearningRun).where(TenantLearningRun.run_type == "recompute_candidates"))
        run_status = run.status if run else ""
        failure_detail = run.failure_detail if run else ""
        trace_id = run.trace_id if run else ""
        session.commit()

    assert run is not None
    assert run_status == "failed"
    assert failure_detail == "quality rule engine unavailable"
    assert trace_id.startswith("candidate-recompute-")


def test_rebuild_failure_writes_visible_failed_run(monkeypatch) -> None:
    original_scalars = Session.scalars

    def broken_scalars(session, statement, *args, **kwargs):
        if "tenant_learning_samples" in str(statement):
            raise RuntimeError("accepted sample query unavailable")
        return original_scalars(session, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalars", broken_scalars)

    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TenantLearningProfile(tenant_id=1, profile_version=3, status="active"))
        session.commit()

        with pytest.raises(ValueError, match="accepted sample query unavailable"):
            rebuild_profile(session, 1, actor="tester", reason="重建失败取证")

        run = session.scalar(select(TenantLearningRun).where(TenantLearningRun.run_type == "rebuild"))
        run_status = run.status if run else ""
        failure_detail = run.failure_detail if run else ""
        trace_id = run.trace_id if run else ""
        profile_version = run.profile_version if run else 0
        session.commit()

    assert run is not None
    assert run_status == "failed"
    assert failure_detail == "accepted sample query unavailable"
    assert trace_id == "profile-rebuild-4-failed"
    assert profile_version == 4
